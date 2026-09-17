"""晋级预测 API — 连板梯队+晋级概率"""

import asyncio
import json
import re
import time
from bisect import bisect_left, bisect_right
from collections import defaultdict
from copy import deepcopy
from datetime import date, datetime, timedelta
from math import exp, isfinite, log, log1p

from fastapi import APIRouter, Depends
from sqlalchemy import and_, desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.tenbagger import (
    _build_stock_rows,
    _bull_rank,
    _get_market_breadth_context,
    _get_latest_persisted_dashboard_snapshot,
    _main_wave_pullback_shape_stats,
    _persist_dashboard_snapshot,
    _score_momentum_shakeout_kline_pattern,
    prewarm_anomaly_snapshot,
)
from app.core.data_date import resolve_latest_trade_date
from app.data.auction_evidence import auction_context_complete, auction_evidence_status
from app.core.price_limit_rules import is_limit_up_change, limit_up_change_threshold
from app.core.trade_calendar import is_official_closed_day, trade_calendar
from app.db.session import get_db
from app.core.stock_tagger import stock_tagger
from app.models.stock import (
    AuctionData,
    LimitUpPool,
    MarketSentiment,
    SectorInfo,
    SectorPersistence,
    FundFlow,
    StockKline,
    StockSectorMapping,
    StockSpot,
    StockTag,
)
from app.models.news import FinanceNews
from app.models.sector import SectorLifecycle
from app.models.signal import PromotionPredictionRecord
from app.models.governance import TradeCalendarModel
from app.promotion.outcome_evidence import next_recorded_trade_day, formal_outcome_bar_error
from app.promotion.deployment import apply_active_promotion_overlay
from app.promotion.direction_research import annotate_direction_research
from app.promotion.modeling.daily_consumer import prepare_daily_hist_context, attach_daily_hist_evidence
from app.promotion.labels import PROMOTION_LABEL_VERSION, promotion_event_label
from app.promotion.regime import build_market_regime_snapshot
from app.promotion.persistence import record_promotion_predictions as _persist_promotion_predictions
from app.promotion.ledger import ScheduleBatch
from app.promotion.snapshot_identity import (
    normalize_snapshot_context as _normalize_prediction_snapshot_context,
    normalize_snapshot_source as _normalize_prediction_snapshot_source,
    parse_snapshot_recorded_at as _parse_prediction_snapshot_recorded_at,
    snapshot_batch_key_from_factors as _prediction_snapshot_batch_key_from_factors,
)
from app.promotion.versioning import get_promotion_model_identity, validate_probability
from app.news.catalyst import (
    classify_news_event,
    load_direct_stock_catalyst_map,
    news_source_weight,
    score_news_catalyst,
)
from app.signal.limit_up_tracker import LimitUpTracker
from app.signal.long_cycle_profile import build_long_cycle_profile
from app.signal.launch_precursors import (
    FEATURE_VERSION as LAUNCH_PRECURSOR_FEATURE_VERSION,
    apply_logit_delta,
    build_funding_preheat_context,
    build_low_base_sector_ignition_context,
    build_stock_launch_profile,
)
from app.signal.dragon_head import THEME_REASON_FAMILIES

router = APIRouter()

tracker = LimitUpTracker()
CONFIDENCE_SCORES = {"high": 0.85, "medium": 0.65, "low": 0.45}
CONFIDENCE_LABELS = {"high": "高", "medium": "中", "low": "低"}
SETUP_GRADE_RANK = {"B类观察候选": 0, "A2 盘口确认后执行": 1, "A1 可直接执行": 2}
FIRST_BOARD_EVENT_BONUS = {
    "capital": 0.07,
    "breakthrough": 0.06,
    "rapid_rise": 0.03,
    "stealth_setup": 0.04,
    "mainline_relay": 0.04,
    "news_catalyst": 0.075,
    "auction_surge": 0.065,
    "mainline_spread": 0.055,
    "pre_board_probe": 0.055,
    "oversold_reversal": 0.045,
    "trend_breakout": 0.035,
    # 低位本身不加分，只有“中低位修复活跃 + 主营行业点火”组合才获得
    # 与历史 lift 相匹配的小额排序权重。
    "low_base_sector_ignition": 0.02,
}
FIRST_BOARD_TRIGGER_EVENT_TYPES = {
    "capital",
    "breakthrough",
    "rapid_rise",
    "stealth_setup",
    "mainline_relay",
    "news_catalyst",
    "auction_surge",
    "mainline_spread",
    "pre_board_probe",
    "oversold_reversal",
    "trend_breakout",
}
from app.promotion.route_contract import ROUTE_LABELS as FIRST_BOARD_ROUTE_LABELS
FIRST_BOARD_WATCH_BUCKET_ORDER = (
    "support_squeeze_watch",
    "support_squeeze_start",
    "news_catalyst_start",
    "auction_surge_start",
    "mainline_spread_start",
    "pre_board_probe_start",
    "oversold_reversal_start",
    "fresh_mainline_start",
    "fresh_relay_start",
    "platform_relaunch",
    "mainline_relay",
    "quiet_setup",
    "other",
)
FIRST_BOARD_WATCH_BUCKET_LABELS = {
    "support_squeeze_watch": "支撑位观察预备",
    "support_squeeze_start": "低位支撑首波",
    "news_catalyst_start": "消息催化首板",
    "auction_surge_start": "竞价高开强攻",
    "mainline_spread_start": "主线扩散补涨",
    "pre_board_probe_start": "涨停试盘首板",
    "oversold_reversal_start": "跌后反包首板",
    "fresh_mainline_start": "主线首波点火",
    "fresh_relay_start": "分支卡位热启动",
    "platform_relaunch": "平台二次点火",
    "mainline_relay": "主线补涨",
    "quiet_setup": "静默蓄势",
    "other": "观察补位",
}
FIRST_BOARD_RANK_LANE_ORDER = (
    "inverse_early_attack",
    "news_catalyst",
    "trend_reclaim",
    "auction_surge",
    "mainline_spread",
    "low_absorb_halfway",
)
FIRST_BOARD_RANK_LANE_LABELS = {
    "inverse_early_attack": "逆市早盘强攻首板",
    "news_catalyst": "消息催化首板",
    "trend_reclaim": "强趋势首阴回收",
    "auction_surge": "竞价高开强攻",
    "mainline_spread": "主线扩散补涨首板",
    "low_absorb_halfway": "低吸/半路观察池",
}
FIRST_BOARD_LEGACY_OBSERVATION_RECORD_ROUTES = set()
FIRST_BOARD_WATCH_BUCKET_DESCRIPTIONS = {
    "support_squeeze_watch": "支撑区已经缩量沉淀，但还差承接、涨幅或确认K进一步共振，先按观察预备跟踪。",
    "support_squeeze_start": "低位支撑区已经稳住，既可能继续缩量沉淀，也可能直接走首次放量启动，重点看支撑不破和启动节奏。",
    "news_catalyst_start": "有公告、新闻或产业事件直接催化，重点看消息硬度、竞价反馈和同题材扩散，而不是只看历史首波记忆。",
    "auction_surge_start": "竞价已经高开放量或接近涨停抢筹，重点看开盘后承接、回落幅度和板块同步性。",
    "mainline_spread_start": "强主线扩散到分支补涨，重点看分支辨识度、补涨位置和主线持续性。",
    "pre_board_probe_start": "前一日出现冲板试盘、放量上攻或临近涨停但未封，重点看次日竞价是否补确认。",
    "oversold_reversal_start": "弱市里前一日恐慌释放后具备题材或盘口修复条件，重点看次日是否弱转强反包。",
    "fresh_mainline_start": "没有历史涨停记忆，但主线首次点火已经很明显，重点看主线强度、资金确认和板块带动性。",
    "fresh_relay_start": "没有历史涨停记忆，更像分支里的卡位或补涨热启动，重点看卡位时点和分支辨识度。",
    "platform_relaunch": "有首波记忆和平台压缩，等一次放量突破把节奏切进主升浪。",
    "mainline_relay": "主线还在，个股处在补涨或卡位阶段，关键看承接和分支地位能否抬升。",
    "quiet_setup": "结构在变好，但还需要更清晰的量价确认，属于偏右侧前夜的静默筹备。",
    "other": "暂未形成明确的首板盘感分层，先留在观察补位区跟踪。",
}
SUPPORT_SQUEEZE_SPRINT_LABEL = "支撑位首波冲刺"
SUPPORT_SQUEEZE_WATCH_LABEL = "支撑位观察预备"
FIRST_BOARD_PRE_SPRINT_LABEL = "准冲刺"
FIRST_BOARD_WEAK_WATCH_LABEL = "弱观察"
FIRST_BOARD_DIAGNOSTIC_REASON_ORDER = (
    "缺少首波记忆",
    "主线强度不足",
    "量能不够",
    "K线结构不够",
    "其他约束",
)
LIMIT_UP_PLATFORM_DIAGNOSTIC_REASON_ORDER = (
    "离板锚点仍偏远",
    "还差十字星确认",
    "还差量窒息",
)
FIRST_BOARD_DIAGNOSTIC_PLAYBOOKS = {
    "缺少首波记忆": (
        "优先把首波记忆分抬到 28+；如果记忆暂时不够，至少要让主线/承接/牛股分同时形成共振。",
        "静默蓄势票要么有历史辨识度，要么得在主线回流时明显站到分支前排；如果想走无记忆热启动，就拿更强的资金点火和突破质量来换。",
    ),
    "主线强度不足": (
        "先看主线强度和题材连续性，热路线至少要回到强主线阈值附近再考虑。",
        "如果板块本身不扩散，再好的个股结构也容易只停留在观察状态。",
    ),
    "量能不够": (
        "优先补承接、净流入和量比，至少先满足一条明确的主动资金推进线。",
        "静默蓄势票还要兼顾量比区间，不是越大越好，最好回到对应路由的理想区间。",
    ),
    "K线结构不够": (
        "先把结构确认项补齐，再看离前高距离和平台压缩是否到位。",
        "这类票通常不是不能做，而是节奏没到临门一脚，先等结构继续成熟。",
    ),
    "其他约束": (
        "先确认是否具备有效触发，再回头看主线、量能和结构三件套。",
    ),
}
LIMIT_UP_PLATFORM_DIAGNOSTIC_PLAYBOOKS = {
    "离板锚点仍偏远": (
        "先看价格能不能重新贴回最近涨停锚点/前高附近，再谈十字星和量窒息。",
        "这类票不是完全没戏，而是位置还没走回资金愿意二次点火的舒服区。",
    ),
    "还差十字星确认": (
        "位置已经接近了，下一步重点等一根更干净的缩量十字星或确认小阳线。",
        "如果已经有十字星影子，但还没站成板附近确认K，就继续等结构硬化。",
    ),
    "还差量窒息": (
        "先让平台量能再缩一截，压回静默区之后，再看弱转强。",
        "这类票通常差的不是位置，而是筹码还没完全沉下来。",
    ),
}
FIRST_BOARD_DIAGNOSTIC_REASON_TAKEAWAYS = {
    "缺少首波记忆": "今天更像辨识度门槛日，没有首波记忆或主线共振的票，很难直接挤进首板主池；除非能走出足够强的无记忆热启动。",
    "主线强度不足": "今天首板更偏主线聚焦，题材不强化时，单票结构再漂亮也容易停留在观察层。",
    "量能不够": "今天首板更看资金确认，承接和量能没站上阈值前，系统会继续把票挡在门外。",
    "K线结构不够": "今天首板更像等结构成熟的市场，节奏没到临门一脚之前，宁可先放观察。",
    "其他约束": "今天首板筛选更偏谨慎，先解决最前面的触发或风控约束，再谈进池。",
}
FIRST_BOARD_MAIN_PROBABILITY_NAME = "次日首板概率（低基准率）"
FIRST_BOARD_5D_PROBABILITY_NAME = "5日内首板概率"
SECOND_BOARD_MAIN_PROBABILITY_NAME = "次日晋级二板概率"
PROMOTION_SENTIMENT_MAP = {
    "climax": "climax",
    "divergence": "divergence",
    "recovery": "recovery",
    "freezing": "freezing",
    "亢奋": "climax",
    "分歧": "divergence",
    "修复": "recovery",
    "冰点": "freezing",
}
ALLOWED_MAINLINE_SECTOR_TYPES = {"concept", "industry", "sw_l1", "sw_l2", "sw_l3"}
GENERIC_RELATIONSHIP_SECTOR_NAMES = {
    "沪股通",
    "深股通",
    "融资融券",
    "转融券标的",
}
STRICT_FIRST_BOARD_MIN_BULL_SCORE = 68.0
STRICT_FIRST_BOARD_MIN_SUPPORT_STRENGTH = 58.0
STRICT_FIRST_BOARD_MIN_SECTOR_STRENGTH = 55.0
STRICT_FIRST_BOARD_MIN_SECTOR_CONTINUITY = 0.34
STRICT_FIRST_BOARD_MIN_KLINE_SCORE = 0.52
STRICT_FIRST_BOARD_MIN_INTRADAY_KLINE_SCORE = 0.56
FRESH_HOT_FIRST_BOARD_MAX_MEMORY_SCORE = 28.0
FRESH_HOT_FIRST_BOARD_MIN_BULL_SCORE = 58.0
FRESH_HOT_FIRST_BOARD_MIN_SUPPORT_STRENGTH = 60.0
FRESH_HOT_FIRST_BOARD_MIN_SECTOR_STRENGTH = 42.0
FRESH_HOT_FIRST_BOARD_MIN_SECTOR_CONTINUITY = 0.16
FRESH_HOT_FIRST_BOARD_MIN_KLINE_SCORE = 0.44
FRESH_HOT_FIRST_BOARD_MIN_INTRADAY_KLINE_SCORE = 0.48
FRESH_HOT_FIRST_BOARD_MIN_MAIN_INFLOW_PCT = 7.0
FRESH_HOT_FIRST_BOARD_MIN_BREAKTHROUGH_VOLUME_RATIO = 1.5
FRESH_HOT_FIRST_BOARD_MIN_CHANGE_PCT = 2.0
FRESH_HOT_FIRST_BOARD_MAX_CHANGE_PCT = 8.2
HOT_MAINLINE_RELAY_MIN_SECTOR_STRENGTH = 58.0
HOT_MAINLINE_RELAY_MIN_LIMIT_UP_COUNT = 3
HOT_MAINLINE_RELAY_MIN_CHANGE_PCT = 0.6
HOT_MAINLINE_RELAY_MAX_CHANGE_PCT = 8.8
HOT_MAINLINE_RELAY_MIN_VOLUME_RATIO = 0.75
HOT_MAINLINE_RELAY_MAX_TURNOVER = 24.0
HOT_MAINLINE_RELAY_SCAN_LIMIT = 700
BROAD_ROTATION_FIRST_BOARD_SCAN_LIMIT = 900
BROAD_ROTATION_FIRST_BOARD_MIN_SECTOR_STRENGTH = 46.0
BROAD_ROTATION_FIRST_BOARD_MIN_ROTATION_SCORE = 54.0
BROAD_ROTATION_FIRST_BOARD_MIN_CHANGE_PCT = -6.5
BROAD_ROTATION_FIRST_BOARD_MIN_VOLUME_RATIO = 0.25
SECTOR_WASHOUT_WATCH_MIN_CHANGE_PCT = -10.5
SECTOR_WASHOUT_WATCH_MAX_CHANGE_PCT = -1.0
SECTOR_WASHOUT_WATCH_MIN_CURRENT_LIMIT_UPS = 2
SECTOR_WASHOUT_WATCH_MIN_CONSECUTIVE_DAYS = 4
SECTOR_WASHOUT_WATCH_MIN_RECENT_PEAK_STRENGTH = 45.0
SECTOR_WASHOUT_WATCH_MIN_RECENT_ACTIVE_DAYS = 3
SECTOR_WASHOUT_WATCH_MIN_RECENT_LIMIT_UP_SUM = 8
SECTOR_WASHOUT_WATCH_MAX_MEMBER_COUNT = 400
FRESH_MAINLINE_START_MIN_SECTOR_STRENGTH = 62.0
FRESH_MAINLINE_START_MIN_SECTOR_CONTINUITY = 0.34
FRESH_MAINLINE_START_MIN_BULL_SCORE = 66.0
FRESH_MAINLINE_START_MIN_SUPPORT_STRENGTH = 64.0
STEALTH_FIRST_BOARD_MIN_BULL_SCORE = 74.0
STEALTH_FIRST_BOARD_MIN_SUPPORT_STRENGTH = 55.0
STEALTH_FIRST_BOARD_MIN_SECTOR_STRENGTH = 48.0
STEALTH_FIRST_BOARD_MIN_SECTOR_CONTINUITY = 0.28
STEALTH_FIRST_BOARD_MIN_KLINE_SCORE = 0.62
STEALTH_FIRST_BOARD_MIN_INTRADAY_KLINE_SCORE = 0.66
QUIET_FIRST_BOARD_MIN_BULL_SCORE = 60.0
QUIET_FIRST_BOARD_MIN_SUPPORT_STRENGTH = 40.0
QUIET_FIRST_BOARD_MIN_SECTOR_STRENGTH = 20.0
QUIET_FIRST_BOARD_MIN_SECTOR_CONTINUITY = 0.1
QUIET_FIRST_BOARD_MIN_KLINE_SCORE = 0.68
QUIET_FIRST_BOARD_MIN_INTRADAY_KLINE_SCORE = 0.72
FIRST_BOARD_DISPLAY_MIN_COUNT = 6
PLATFORM_RELAUNCH_MIN_DAYS_SINCE_LAST_LIMIT_UP = 3
PLATFORM_RELAUNCH_MAX_DAYS_SINCE_LAST_LIMIT_UP = 20
PLATFORM_RELAUNCH_MAX_ANCHOR_GAP_PCT = 4.2
PLATFORM_RELAUNCH_DOJI_MAX_ANCHOR_GAP_PCT = 3.4
PLATFORM_RELAUNCH_KLINE_SCORE_RELIEF = 0.06
SUPPORT_BASE_KLINE_SCORE_RELIEF = 0.04
SUPPORT_VOLUME_RELEASE_MIN_RATIO = 1.12
SUPPORT_VOLUME_RELEASE_MAX_RATIO = 2.8
SUPPORT_VOLUME_RELEASE_MIN_CHANGE_PCT = 0.6
SUPPORT_VOLUME_RELEASE_SPRINT_MIN_CHANGE_PCT = 1.0
SUPPORT_VOLUME_RELEASE_MAX_CHANGE_PCT = 6.5
SUPPORT_VOLUME_RELEASE_MIN_SUPPORT_STRENGTH = 46.0
NEWS_CATALYST_LOOKBACK_DAYS = 3
NEWS_CATALYST_MIN_SCORE = 46.0
AUCTION_SURGE_MIN_SCORE = 45.0
AUCTION_SURGE_MIN_OPEN_CHANGE = 2.6
# 50个已完成竞价日的主板逐日样本显示：剔除当日已经涨停的股票后，
# 2.6%~6.0%高开且增量成交完整的次日首板率约5%，而6%以上只约1%。
# 高开过度更像当日兑现而不是次日低风险买点，因此正式首板池封顶6%。
AUCTION_SURGE_MAX_OPEN_CHANGE = 6.0
MAINLINE_SPREAD_MIN_SECTOR_STRENGTH = 62.0
MAINLINE_SPREAD_MIN_SECTOR_CONTINUITY = 0.22
MAINLINE_SPREAD_MIN_CHANGE_PCT = 0.6
MAINLINE_SPREAD_MAX_CHANGE_PCT = 8.8
BROAD_ROTATION_MAINLINE_MIN_SECTOR_STRENGTH = 50.0
BROAD_ROTATION_MAINLINE_MIN_SECTOR_CONTINUITY = 0.14
BROAD_ROTATION_MAINLINE_MIN_ROTATION_SCORE = 52.0
BROAD_ROTATION_MAINLINE_MIN_LIMIT_UP_COUNT = 6
BROAD_ROTATION_MAINLINE_STRONG_LIMIT_UP_COUNT = 8
BROAD_ROTATION_REVERSAL_MIN_OVERSOLD_SCORE = 88.0
BROAD_ROTATION_REVERSAL_MIN_SUPPORT_SCORE = 74.0
PRE_BOARD_PROBE_MIN_SCORE = 52.0
OVERSOLD_REVERSAL_MIN_SCORE = 50.0
PRE_BOARD_SCAN_LIMIT = 2000
PROMOTION_MIN_FIRST_BOARD_SNAPSHOT_RECORDS = 10
PROMOTION_MIN_SECOND_BOARD_SNAPSHOT_RECORDS = 3
# 正式概率榜同时承担“可执行候选”和“只用于预测学习的形态种子”两种职责。
# 可执行性仍由 trade_ready / prediction_watch_only 独立约束；主榜保留12个
# 多赛道样本，避免把低基准率首板预测压缩成仅6只后进一步损伤召回。
PROMOTION_MAX_FORMAL_RANKED = 12
# 正式 Top12 负责精排审计，Top30 宽召回独立展示，不能再把短名单数量
# 误解成市场首板总数，也不能用交易执行闸门删掉预测样本。
PROMOTION_MAX_RECALL_RANKED = 30
PROMOTION_PAGE_CACHE_MIN_CANDIDATE_CAPACITY = 12
STOCK_CODE_RE = re.compile(r"(?<!\d)(?:[0368]\d{5})(?!\d)")
PLATFORM_CYCLE_CONFIGS = (
    {
        "type": "long",
        "label": "长平台",
        "min_days": 31,
        "max_days": 60,
        "max_range_pct": 18.0,
        "max_shrink_ratio": 0.76,
        "max_suffocation_ratio": 0.62,
        "max_recent_volume_ceiling_ratio": 0.74,
        "max_overhead_gap_pct": 4.2,
        "max_recent_amplitude_pct": 3.8,
    },
    {
        "type": "mid",
        "label": "中平台",
        "min_days": 16,
        "max_days": 30,
        "max_range_pct": 12.5,
        "max_shrink_ratio": 0.8,
        "max_suffocation_ratio": 0.68,
        "max_recent_volume_ceiling_ratio": 0.82,
        "max_overhead_gap_pct": 3.6,
        "max_recent_amplitude_pct": 4.2,
    },
    {
        "type": "short",
        "label": "短平台",
        "min_days": 8,
        "max_days": 15,
        "max_range_pct": 8.8,
        "max_shrink_ratio": 0.84,
        "max_suffocation_ratio": 0.74,
        "max_recent_volume_ceiling_ratio": 0.88,
        "max_overhead_gap_pct": 3.0,
        "max_recent_amplitude_pct": 4.6,
    },
)
PROMOTION_SIGNAL_STATUS_INTRADAY_PREVIEW = "intraday_preview"
PROMOTION_SIGNAL_STATUS_CLOSE_CONFIRMED = "close_confirmed"
PROMOTION_SIGNAL_STATUS_LABELS = {
    PROMOTION_SIGNAL_STATUS_INTRADAY_PREVIEW: "盘中预判",
    PROMOTION_SIGNAL_STATUS_CLOSE_CONFIRMED: "收盘确认",
}
PROMOTION_INTERNAL_RECORD_LIMIT = 2000
PROMOTION_PAGE_CACHE_TTL_SECONDS = 300
PROMOTION_SCHEDULE_CACHE_MAX_AGE_SECONDS = 60 * 60
PROMOTION_REVIEW_DEFAULT_DAYS = 10
PROMOTION_REVIEW_MAX_DAYS = 30
PROMOTION_MODEL_IDENTITY = get_promotion_model_identity()
# Backward-compatible alias used by existing persistence and tests.  The active
# production version always remains the champion in legacy/shadow/compare mode.
PROMOTION_MODEL_VERSION = PROMOTION_MODEL_IDENTITY.active_model_version
PROMOTION_LAUNCH_COHORT_LABELS = {
    "all_ranked_first_board": "全部首板主榜",
    "launch_profile": "中低位修复活跃",
    "funding_preheat": "三日资金预热",
    "primary_industry_ignition": "主营行业点火",
    "low_base_industry": "低位行业点火组合",
    "low_base_industry_funding": "低位行业+资金共振",
    "direct_high_impact_news": "个股高影响消息",
    "direct_repeated_news": "个股重复消息确认",
    "persistent_funding_outflow": "持续资金流出风险",
}
PROMOTION_CALIBRATION_VERSION = "versioned_recent_beta_binomial_v3"
PROMOTION_FIRST_BOARD_TEMPORAL_CALIBRATION_VERSION = "temporal_logit_v1_blend75_route25"
PROMOTION_CANDIDATES_SNAPSHOT_KEY = "promotion_candidates"
PROMOTION_CANONICAL_CLOSE_CONTEXTS = ("promotion_2000", "promotion_1510")
PROMOTION_OPEN_CONFIRM_CONTEXTS = ("promotion_0925", "promotion_0935")
# 09:35 只能看到开盘初段，主线常在首批涨停扩散后才成形。后续时点仍写
# 不可变正式批次，供策略C消费；B/D继续只消费开盘确认批次。
PROMOTION_MAINLINE_REFRESH_CONTEXTS = (
    "promotion_1000",
    "promotion_1030",
    "promotion_1305",
    "promotion_1400",
    "promotion_1430",
)
PROMOTION_INTRADAY_CONTEXTS = (
    *PROMOTION_OPEN_CONFIRM_CONTEXTS,
    *PROMOTION_MAINLINE_REFRESH_CONTEXTS,
)
PROMOTION_LEARNING_RECENT_DAYS = 90
PROMOTION_LEGACY_SAMPLE_CAP = 24
PROMOTION_MAX_UNCONFIRMED_FIRST_BOARD_RANKED = 4
PROMOTION_PAGE_CANDIDATES_SNAPSHOT_KEY = "promotion_candidates_page"
PROMOTION_FIRST_BOARD_MAX_RESIDUAL_WEIGHT = 0.40
PROMOTION_SECOND_BOARD_MAX_RESIDUAL_WEIGHT = 0.55
PROMOTION_LEARNING_MIN_RECALL_SUCCESS = 8
PROMOTION_LEARNING_MAX_RECALL_RANK_BOOST = 8.0
DRAGON_TIGER_CACHE_TTL_SECONDS = 10 * 60
DRAGON_TIGER_FETCH_TIMEOUT_SECONDS = 20
DRAGON_TIGER_DETAIL_TIMEOUT_SECONDS = 12
_PROMOTION_PAGE_CANDIDATES_CACHE: dict[tuple[int, int, bool, str, int], tuple[float, dict]] = {}
_PROMOTION_LATEST_CANDIDATES_CACHE: dict[tuple[str, int], tuple[float, dict]] = {}
_PROMOTION_LEARNING_REVIEW_CACHE: dict[tuple[str, int, int], tuple[float, dict]] = {}
_DRAGON_TIGER_DAILY_CACHE: dict[str, tuple[float, dict[str, list[dict]]]] = {}
_DRAGON_TIGER_CONTEXT_CACHE: dict[tuple[str, str], tuple[float, dict]] = {}
_PROMOTION_LEARNING_STORAGE_READY = False
_PROMOTION_OFFICIAL_CONTEXT_WINDOWS = {
    "promotion_1510": ((15, 5), (16, 30)),
    "promotion_2000": ((19, 50), (21, 30)),
    # 09:25 窗口放宽到 09:40，覆盖重启补跑(09:25-09:35)的竞价语义快照；
    # 收盘时点仍由 15:10/20:00 窗口和交易日锚点双重隔离。
    "promotion_0925": ((9, 20), (9, 40)),
    "promotion_0935": ((9, 30), (9, 50)),
    "promotion_1000": ((9, 55), (10, 15)),
    "promotion_1030": ((10, 25), (10, 45)),
    "promotion_1305": ((13, 0), (13, 20)),
    "promotion_1400": ((13, 55), (14, 15)),
    "promotion_1430": ((14, 25), (14, 45)),
}


async def _validate_official_snapshot_clock(
    snapshot_context: str,
    recorded_at: datetime,
    reference_trade_dates: set[date],
) -> None:
    """Bind an official run to its declared trading session and cutoff window."""

    context = _normalize_prediction_snapshot_context(
        snapshot_context,
        source="schedule",
    )
    window = _PROMOTION_OFFICIAL_CONTEXT_WINDOWS.get(context)
    if window is None:
        raise ValueError(f"unsupported official snapshot_context: {context or '--'}")
    clock = (recorded_at.hour, recorded_at.minute)
    if not window[0] <= clock <= window[1]:
        raise ValueError(
            f"official {context} snapshot must run within "
            f"{window[0][0]:02d}:{window[0][1]:02d}-"
            f"{window[1][0]:02d}:{window[1][1]:02d}"
        )
    if not reference_trade_dates:
        raise ValueError("official snapshot has no reference trade date")
    if context in PROMOTION_CANONICAL_CLOSE_CONTEXTS:
        expected_dates = set(reference_trade_dates)
    else:
        # The morning run can legitimately mix two lane anchors: live first-board
        # candidates are dated to the current auction session, while second-board
        # candidates still originate from the previous close. Both must resolve
        # to the one session in which this 09:25/09:35 snapshot is recorded.
        expected_dates = set()
        for trade_day in reference_trade_dates:
            expected_dates.add(
                recorded_at.date()
                if trade_day == recorded_at.date()
                else await trade_calendar.next_trade_day(trade_day)
            )
    if expected_dates != {recorded_at.date()}:
        raise ValueError(
            "official snapshot clock does not match its reference trading session: "
            f"context={context}, recorded_at={recorded_at.isoformat(timespec='seconds')}, "
            f"expected_dates={sorted(str(item) for item in expected_dates)}"
        )


def _attach_frozen_market_regime(
    candidates: list[dict],
    regime_snapshot: dict | None,
) -> list[dict]:
    """Freeze the exact regime evidence available to this prediction execution."""

    if not regime_snapshot or not regime_snapshot.get("primary_regime"):
        return candidates
    result: list[dict] = []
    for item in candidates:
        factors = dict(item.get("probability_factors") or {})
        factors.update(
            {
                "market_regime": regime_snapshot["primary_regime"],
                "market_regime_snapshot_id": regime_snapshot.get("id"),
                "market_regime_version": regime_snapshot.get("regime_version"),
                "market_regime_data_version": regime_snapshot.get("data_version"),
                "market_regime_as_of_at": regime_snapshot.get("as_of_at"),
                "market_regime_quality_status": regime_snapshot.get("quality_status"),
            }
        )
        result.append({**item, "probability_factors": factors})
    return result


def _safe_float(value, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if isfinite(result) else default
    except (TypeError, ValueError):
        return default


def _safe_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _percentile_rank_map(values: dict[str, float]) -> dict[str, float]:
    """把不同量纲的横截面指标转成可比较的0-100分，平均处理并列值。"""
    normalized = {
        str(key or ""): _safe_float(value)
        for key, value in values.items()
        if str(key or "")
    }
    if not normalized:
        return {}
    ordered = sorted(normalized.values())
    if len(ordered) == 1:
        only_key = next(iter(normalized))
        return {only_key: 50.0}
    denominator = len(ordered) - 1
    ranks: dict[str, float] = {}
    for key, value in normalized.items():
        left = bisect_left(ordered, value)
        right = bisect_right(ordered, value) - 1
        average_rank = (left + right) / 2.0
        ranks[key] = round(average_rank / denominator * 100.0, 2)
    return ranks


def _safe_percent_change(current: float, base: float) -> float:
    if current <= 0 or base <= 0:
        return 0.0
    return (current / base - 1.0) * 100.0


def _safe_absolute_percent_gap(current: float, anchor: float) -> float:
    if current <= 0 or anchor <= 0:
        return 999.0
    return abs(current - anchor) / anchor * 100.0


def _clamp_probability(value: float) -> float:
    return round(max(0.01, min(0.95, value)), 3)


def _clamp_score(value: float) -> float:
    return round(max(0.0, min(100.0, value)), 2)


def _inverse_score(value: float, *, good: float, bad: float) -> float:
    if value <= good:
        return 100.0
    if value >= bad:
        return 0.0
    if bad <= good:
        return 0.0
    return _clamp_score((bad - value) / (bad - good) * 100.0)


def _range_score(
    value: float,
    *,
    ideal_low: float,
    ideal_high: float,
    min_value: float,
    max_value: float,
) -> float:
    if min_value >= ideal_low or max_value <= ideal_high:
        return 0.0
    if ideal_low <= value <= ideal_high:
        return 100.0
    if value < ideal_low:
        return _clamp_score((value - min_value) / max(ideal_low - min_value, 0.1) * 100.0)
    return _clamp_score((max_value - value) / max(max_value - ideal_high, 0.1) * 100.0)


def _score_to_probability(score: float, *, pivot: float, spread: float) -> float:
    normalized = 1.0 / (1.0 + exp(-(score - pivot) / max(spread, 0.1)))
    return _clamp_probability(normalized)


def _normalize_promotion_sentiment(value: str | None) -> str:
    normalized = str(value or "").strip().lower()
    return PROMOTION_SENTIMENT_MAP.get(normalized, PROMOTION_SENTIMENT_MAP.get(str(value or "").strip(), "recovery"))


def _confidence_from_probability(probability: float) -> tuple[str, float, str]:
    if probability >= 0.65:
        level = "high"
    elif probability >= 0.45:
        level = "medium"
    else:
        level = "low"
    return level, CONFIDENCE_SCORES[level], CONFIDENCE_LABELS[level]


def _normalize_dragon_tiger_code(value) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) >= 6:
        return digits[-6:]
    return raw.zfill(6) if raw.isdigit() else raw


def _dragon_tiger_empty_context(reason: str = "未上龙虎榜") -> dict:
    return {
        "is_listed": False,
        "detail_available": False,
        "source": "eastmoney",
        "summary": reason,
        "reasons": [],
        "interpretation": "",
        "net_buy_amount": 0.0,
        "buy_amount": 0.0,
        "sell_amount": 0.0,
        "trade_amount": 0.0,
        "market_amount": 0.0,
        "net_buy_pct": 0.0,
        "trade_amount_pct": 0.0,
        "turnover": 0.0,
        "institution_net_amount": 0.0,
        "northbound_net_amount": 0.0,
        "broker_branch_net_amount": 0.0,
        "positive_member_count": 0,
        "negative_member_count": 0,
        "top_buy_members": [],
        "top_sell_members": [],
        "member_score": 50.0,
        "probability_delta": 0.0,
        "risk_flags": [],
    }


def _dataframe_records(df) -> list[dict]:
    if df is None or bool(getattr(df, "empty", True)):
        return []
    try:
        return list(df.to_dict("records"))
    except AttributeError:
        return []


def _json_dumps_safe(value) -> str:
    try:
        return json.dumps(value or {}, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return "{}"


def _json_loads_safe(value: str | None, default=None):
    if default is None:
        default = {}
    try:
        return json.loads(value or "")
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


async def _ensure_promotion_learning_storage(db: AsyncSession) -> None:
    """确保预测复盘表存在，避免迁移未执行时学习闭环静默失效。"""
    global _PROMOTION_LEARNING_STORAGE_READY
    if _PROMOTION_LEARNING_STORAGE_READY:
        return

    await db.run_sync(
        lambda sync_session: PromotionPredictionRecord.__table__.create(
            bind=sync_session.get_bind(),
            checkfirst=True,
        )
    )
    _PROMOTION_LEARNING_STORAGE_READY = True


def _dragon_tiger_row_value(row: dict, *keys, default=None):
    for key in keys:
        if key in row and row.get(key) is not None:
            return row.get(key)
    return default


def _dragon_tiger_member_category(name: str) -> str:
    if "机构专用" in name:
        return "institution"
    if "沪股通" in name or "深股通" in name or "港股通" in name:
        return "northbound"
    if "专用" in name:
        return "special"
    return "broker_branch"


def _build_dragon_tiger_member(row: dict, side: str) -> dict:
    name = str(_dragon_tiger_row_value(row, "交易营业部名称", "营业部名称", default="")).strip()
    buy_amount = _safe_float(_dragon_tiger_row_value(row, "买入金额", default=0))
    sell_amount = _safe_float(_dragon_tiger_row_value(row, "卖出金额", default=0))
    net_amount = _safe_float(_dragon_tiger_row_value(row, "净额", "净买额", default=buy_amount - sell_amount))
    return {
        "name": name,
        "side": side,
        "category": _dragon_tiger_member_category(name),
        "buy_amount": round(buy_amount, 2),
        "sell_amount": round(sell_amount, 2),
        "net_amount": round(net_amount, 2),
        "reason": str(_dragon_tiger_row_value(row, "类型", "上榜原因", default="")).strip(),
    }


def _dedupe_dragon_tiger_members(members: list[dict]) -> list[dict]:
    seen: set[tuple] = set()
    deduped: list[dict] = []
    for member in members:
        key = (
            member.get("name"),
            round(_safe_float(member.get("buy_amount")), 2),
            round(_safe_float(member.get("sell_amount")), 2),
            round(_safe_float(member.get("net_amount")), 2),
            member.get("reason"),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(member)
    return deduped


def _format_dragon_tiger_amount(value: float) -> str:
    amount = abs(_safe_float(value))
    if amount >= 1e8:
        return f"{amount / 1e8:.2f}亿"
    if amount >= 1e4:
        return f"{amount / 1e4:.0f}万"
    return f"{amount:.0f}"


def _calc_dragon_tiger_probability_delta(
    *,
    net_buy_amount: float,
    net_buy_pct: float,
    trade_amount_pct: float,
    institution_net: float,
    northbound_net: float,
    broker_branch_net: float,
    positive_member_count: int,
    negative_member_count: int,
) -> tuple[float, list[str]]:
    delta = 0.0
    risk_flags: list[str] = []

    if net_buy_pct >= 8.0:
        delta += 0.03
    elif net_buy_pct >= 4.0:
        delta += 0.018
    elif net_buy_pct <= -8.0:
        delta -= 0.04
        risk_flags.append("龙虎榜净卖出占比偏高")
    elif net_buy_pct <= -4.0:
        delta -= 0.024

    if net_buy_amount >= 100_000_000:
        delta += 0.015
    elif net_buy_amount >= 50_000_000:
        delta += 0.008
    elif net_buy_amount <= -100_000_000:
        delta -= 0.02
        risk_flags.append("龙虎榜净卖出过亿")
    elif net_buy_amount <= -50_000_000:
        delta -= 0.012

    if institution_net >= 30_000_000:
        delta += 0.015
    elif institution_net <= -30_000_000:
        delta -= 0.025
        risk_flags.append("机构席位净卖出")

    if northbound_net >= 30_000_000:
        delta += 0.01
    elif northbound_net <= -30_000_000:
        delta -= 0.015

    if broker_branch_net >= 60_000_000:
        delta += 0.015
    elif broker_branch_net <= -60_000_000:
        delta -= 0.02

    if positive_member_count >= 4 and negative_member_count <= 2:
        delta += 0.01
    if negative_member_count >= 4 and positive_member_count <= 2:
        delta -= 0.01
        risk_flags.append("卖方席位集中")

    if trade_amount_pct >= 45.0 and net_buy_pct < 2.0:
        delta -= 0.008
        risk_flags.append("龙虎榜成交占比高但净买弱")

    return round(max(-0.08, min(0.06, delta)), 3), risk_flags


def _build_dragon_tiger_summary_text(context: dict) -> str:
    if not context.get("is_listed"):
        return str(context.get("summary") or "未上龙虎榜")

    net_buy_amount = _safe_float(context.get("net_buy_amount"))
    direction = "净买入" if net_buy_amount >= 0 else "净卖出"
    parts = [f"龙虎榜{direction}{_format_dragon_tiger_amount(net_buy_amount)}"]

    institution_net = _safe_float(context.get("institution_net_amount"))
    if abs(institution_net) >= 10_000_000:
        parts.append(f"机构{'净买' if institution_net >= 0 else '净卖'}{_format_dragon_tiger_amount(institution_net)}")

    northbound_net = _safe_float(context.get("northbound_net_amount"))
    if abs(northbound_net) >= 10_000_000:
        parts.append(f"北向{'净买' if northbound_net >= 0 else '净卖'}{_format_dragon_tiger_amount(northbound_net)}")

    top_buy_members = context.get("top_buy_members") or []
    if top_buy_members:
        top_name = str(top_buy_members[0].get("name") or "").replace("证券营业部", "")
        if top_name:
            parts.append(f"买一{top_name[:16]}")

    return "，".join(parts)


def _build_dragon_tiger_context(summary_rows: list[dict], buy_rows: list[dict], sell_rows: list[dict]) -> dict:
    if not summary_rows:
        return _dragon_tiger_empty_context()

    selected_row = max(summary_rows, key=lambda row: _safe_float(_dragon_tiger_row_value(row, "龙虎榜成交额", default=0)))
    reasons = [
        str(_dragon_tiger_row_value(row, "上榜原因", default="")).strip()
        for row in summary_rows
        if str(_dragon_tiger_row_value(row, "上榜原因", default="")).strip()
    ]
    reasons = list(dict.fromkeys(reasons))

    buy_members = [_build_dragon_tiger_member(row, "buy") for row in buy_rows]
    sell_members = [_build_dragon_tiger_member(row, "sell") for row in sell_rows]
    buy_members = [member for member in buy_members if member.get("name")]
    sell_members = [member for member in sell_members if member.get("name")]
    all_members = _dedupe_dragon_tiger_members(buy_members + sell_members)

    institution_net = sum(_safe_float(member.get("net_amount")) for member in all_members if member.get("category") == "institution")
    northbound_net = sum(_safe_float(member.get("net_amount")) for member in all_members if member.get("category") == "northbound")
    broker_branch_net = sum(
        _safe_float(member.get("net_amount"))
        for member in all_members
        if member.get("category") in {"broker_branch", "special"}
    )
    positive_member_count = sum(1 for member in all_members if _safe_float(member.get("net_amount")) > 0)
    negative_member_count = sum(1 for member in all_members if _safe_float(member.get("net_amount")) < 0)

    net_buy_amount = _safe_float(_dragon_tiger_row_value(selected_row, "龙虎榜净买额", default=0))
    net_buy_pct = _safe_float(_dragon_tiger_row_value(selected_row, "净买额占总成交比", default=0))
    trade_amount_pct = _safe_float(_dragon_tiger_row_value(selected_row, "成交额占总成交比", default=0))
    probability_delta, risk_flags = _calc_dragon_tiger_probability_delta(
        net_buy_amount=net_buy_amount,
        net_buy_pct=net_buy_pct,
        trade_amount_pct=trade_amount_pct,
        institution_net=institution_net,
        northbound_net=northbound_net,
        broker_branch_net=broker_branch_net,
        positive_member_count=positive_member_count,
        negative_member_count=negative_member_count,
    )

    top_buy_members = sorted(buy_members, key=lambda member: _safe_float(member.get("buy_amount")), reverse=True)[:5]
    top_sell_members = sorted(sell_members, key=lambda member: _safe_float(member.get("sell_amount")), reverse=True)[:5]
    context = {
        "is_listed": True,
        "detail_available": bool(buy_members or sell_members),
        "source": "eastmoney",
        "summary": "",
        "reasons": reasons,
        "interpretation": str(_dragon_tiger_row_value(selected_row, "解读", default="")).strip(),
        "net_buy_amount": round(net_buy_amount, 2),
        "buy_amount": round(_safe_float(_dragon_tiger_row_value(selected_row, "龙虎榜买入额", default=0)), 2),
        "sell_amount": round(_safe_float(_dragon_tiger_row_value(selected_row, "龙虎榜卖出额", default=0)), 2),
        "trade_amount": round(_safe_float(_dragon_tiger_row_value(selected_row, "龙虎榜成交额", default=0)), 2),
        "market_amount": round(_safe_float(_dragon_tiger_row_value(selected_row, "市场总成交额", default=0)), 2),
        "net_buy_pct": round(net_buy_pct, 3),
        "trade_amount_pct": round(trade_amount_pct, 3),
        "turnover": round(_safe_float(_dragon_tiger_row_value(selected_row, "换手率", default=0)), 3),
        "institution_net_amount": round(institution_net, 2),
        "northbound_net_amount": round(northbound_net, 2),
        "broker_branch_net_amount": round(broker_branch_net, 2),
        "positive_member_count": positive_member_count,
        "negative_member_count": negative_member_count,
        "top_buy_members": top_buy_members,
        "top_sell_members": top_sell_members,
        "member_score": _clamp_score(50 + probability_delta * 500),
        "probability_delta": probability_delta,
        "risk_flags": risk_flags,
    }
    context["summary"] = _build_dragon_tiger_summary_text(context)
    return context


async def _load_dragon_tiger_daily_summary(source, trade_date: date) -> dict[str, list[dict]]:
    date_key = trade_date.strftime("%Y%m%d")
    now = time.monotonic()
    cached = _DRAGON_TIGER_DAILY_CACHE.get(date_key)
    if cached and now - cached[0] <= DRAGON_TIGER_CACHE_TTL_SECONDS:
        return cached[1]

    df = await asyncio.wait_for(
        source.get_dragon_tiger_list(start_date=date_key, end_date=date_key),
        timeout=DRAGON_TIGER_FETCH_TIMEOUT_SECONDS,
    )
    rows_by_code: dict[str, list[dict]] = {}
    for row in _dataframe_records(df):
        code = _normalize_dragon_tiger_code(_dragon_tiger_row_value(row, "代码", default=""))
        if not code:
            continue
        rows_by_code.setdefault(code, []).append(row)

    _DRAGON_TIGER_DAILY_CACHE[date_key] = (now, rows_by_code)
    return rows_by_code


async def _load_dragon_tiger_stock_context(source, trade_date: date, code: str, summary_rows: list[dict]) -> dict:
    date_key = trade_date.strftime("%Y%m%d")
    cache_key = (date_key, code)
    now = time.monotonic()
    cached = _DRAGON_TIGER_CONTEXT_CACHE.get(cache_key)
    if cached and now - cached[0] <= DRAGON_TIGER_CACHE_TTL_SECONDS:
        return cached[1]

    buy_rows: list[dict] = []
    sell_rows: list[dict] = []
    try:
        buy_df = await asyncio.wait_for(
            source.get_dragon_tiger_stock_detail(symbol=code, trade_date=date_key, flag="买入"),
            timeout=DRAGON_TIGER_DETAIL_TIMEOUT_SECONDS,
        )
        sell_df = await asyncio.wait_for(
            source.get_dragon_tiger_stock_detail(symbol=code, trade_date=date_key, flag="卖出"),
            timeout=DRAGON_TIGER_DETAIL_TIMEOUT_SECONDS,
        )
        buy_rows = _dataframe_records(buy_df)
        sell_rows = _dataframe_records(sell_df)
    except Exception:
        buy_rows = []
        sell_rows = []

    context = _build_dragon_tiger_context(summary_rows, buy_rows, sell_rows)
    _DRAGON_TIGER_CONTEXT_CACHE[cache_key] = (now, context)
    return context


async def _load_dragon_tiger_context_map(trade_date: date, codes: list[str]) -> dict[str, dict]:
    normalized_codes = list(dict.fromkeys(_normalize_dragon_tiger_code(code) for code in codes if code))
    if not normalized_codes:
        return {}

    try:
        from app.data.sources.eastmoney_source import EastMoneySource

        source = EastMoneySource()
        summary_by_code = await _load_dragon_tiger_daily_summary(source, trade_date)
        listed_codes = [code for code in normalized_codes if summary_by_code.get(code)]
        if not listed_codes:
            return {}

        result: dict[str, dict] = {}
        for code in listed_codes:
            result[code] = await _load_dragon_tiger_stock_context(
                source,
                trade_date,
                code,
                summary_by_code.get(code) or [],
            )
        return result
    except Exception:
        return {}


def _load_cached_dragon_tiger_context_map(trade_date: date, codes: list[str]) -> dict[str, dict]:
    date_key = trade_date.strftime("%Y%m%d")
    now = time.monotonic()
    result: dict[str, dict] = {}
    for code in dict.fromkeys(_normalize_dragon_tiger_code(code) for code in codes if code):
        cached = _DRAGON_TIGER_CONTEXT_CACHE.get((date_key, code))
        if cached and now - cached[0] <= DRAGON_TIGER_CACHE_TTL_SECONDS:
            result[code] = cached[1]
    return result


def _promotion_cache_scope(db: AsyncSession) -> tuple[str, int]:
    try:
        bind = db.get_bind()
        bind_url = str(getattr(bind, "url", "") or "")
        bind_identity = id(bind)
    except Exception:
        bind_url = ""
        bind_identity = 0
    return bind_url, bind_identity


def _promotion_page_cache_key(
    db: AsyncSession,
    candidate_limit: int,
    ranked_candidate_limit: int,
    compact: bool,
) -> tuple[int, int, bool, str, int]:
    bind_url, bind_identity = _promotion_cache_scope(db)
    return candidate_limit, ranked_candidate_limit, compact, bind_url, bind_identity


def _promotion_cached_payload_supports_request(
    payload: dict,
    *,
    candidate_limit: int,
    ranked_limit: int,
) -> bool:
    """仅复用确实覆盖本次上限的规范快照，避免小请求污染后续大列表。"""
    capacity = payload.get("_cache_capacity") or {}
    return (
        _safe_int(capacity.get("candidate_limit")) >= candidate_limit
        and _safe_int(capacity.get("ranked_limit")) >= ranked_limit
    )


def _project_promotion_candidates_payload(
    payload: dict,
    *,
    compact: bool,
    candidate_limit: int | None = None,
    ranked_limit: int | None = None,
) -> dict:
    """按本次请求裁剪缓存快照，并移除首屏不需要的重复重数据。"""
    # 缓存中的 payload 会被不同 limit/compact 请求复用；深拷贝避免状态修复或
    # 数组裁剪反向污染规范快照，造成列表长度和 count 字段互相串线。
    projected = deepcopy(payload)

    normalized_candidate_limit = max(int(candidate_limit), 0) if candidate_limit is not None else None
    normalized_ranked_limit = max(int(ranked_limit), 0) if ranked_limit is not None else None
    if normalized_candidate_limit is not None:
        candidate_count_fields = {
            "first_board_candidates": "first_board_count",
            "first_board_pre_sprint_candidates": "first_board_pre_sprint_count",
            "first_board_watch_candidates": "first_board_watch_count",
            "first_board_weak_watch_candidates": "first_board_weak_watch_count",
            "limit_up_platform_candidates": "limit_up_platform_count",
            "second_board_candidates": "second_board_count",
        }
        for key, count_key in candidate_count_fields.items():
            rows = projected.get(key)
            if not isinstance(rows, list):
                continue
            projected[key] = rows[:normalized_candidate_limit]
            projected[count_key] = len(projected[key])

        watch_group_specs = (
            (
                "first_board_pre_sprint_candidates",
                "first_board_pre_sprint_overview",
                "first_board_pre_sprint_groups",
                "pre_sprint",
            ),
            (
                "first_board_watch_candidates",
                "first_board_watch_overview",
                "first_board_watch_groups",
                "watch",
            ),
            (
                "first_board_weak_watch_candidates",
                "first_board_weak_watch_overview",
                "first_board_weak_watch_groups",
                "weak_watch",
            ),
        )
        for candidate_key, overview_key, groups_key, time_horizon in watch_group_specs:
            rows = projected.get(candidate_key)
            if not isinstance(rows, list):
                continue
            grouped = _build_first_board_watch_groups(
                rows,
                limit_per_group=normalized_candidate_limit,
                time_horizon=time_horizon,
            )
            projected[overview_key] = grouped["overview"]
            projected[groups_key] = grouped["groups"]

    if normalized_ranked_limit is not None:
        formal_limit = min(
            normalized_ranked_limit,
            PROMOTION_MAX_FORMAL_RANKED,
        )
        recall_limit = min(
            normalized_ranked_limit,
            PROMOTION_MAX_RECALL_RANKED,
        )
        first_ranked = projected.get("ranked_first_board_candidates")
        if isinstance(first_ranked, list):
            first_ranked = first_ranked[:formal_limit]
            projected["ranked_first_board_candidates"] = first_ranked
            projected["ranked_first_board_count"] = len(first_ranked)
            projected["ranked_first_board_actionable_count"] = sum(
                1 for item in first_ranked if item.get("prediction_actionable")
            )
            projected["ranked_first_board_abstained_slots"] = max(
                formal_limit - len(first_ranked),
                0,
            )
        first_recall_ranked = projected.get(
            "ranked_first_board_recall_candidates"
        )
        if isinstance(first_recall_ranked, list):
            first_recall_ranked = first_recall_ranked[:recall_limit]
            projected["ranked_first_board_recall_candidates"] = first_recall_ranked
            projected["ranked_first_board_recall_count"] = len(
                first_recall_ranked
            )
        projected["formal_first_board_limit"] = formal_limit
        projected["recall_first_board_limit"] = recall_limit
        second_ranked = projected.get("ranked_second_board_candidates")
        if isinstance(second_ranked, list):
            second_ranked = second_ranked[:formal_limit]
            projected["ranked_second_board_candidates"] = second_ranked
            projected["ranked_second_board_count"] = len(second_ranked)
            projected["ranked_second_board_actionable_count"] = sum(
                1 for item in second_ranked if item.get("trade_ready")
            )
        debug = projected.get("debug")
        if isinstance(debug, dict) and isinstance(debug.get("mixed_ranked_candidates"), list):
            debug["mixed_ranked_candidates"] = debug["mixed_ranked_candidates"][:formal_limit]

    # 持久化页面快照可能在早盘/午休生成；读取时依快照时点重算状态，
    # 避免旧版把 lunch_break 误标成“收盘确认”后继续污染页面。
    snapshot_clock = str(projected.get("snapshot_time") or "").strip()
    candidate_keys = (
        "first_board_candidates",
        "first_board_pre_sprint_candidates",
        "first_board_watch_candidates",
        "first_board_weak_watch_candidates",
        "limit_up_platform_candidates",
        "second_board_candidates",
        "ranked_first_board_candidates",
        "ranked_first_board_recall_candidates",
        "ranked_second_board_candidates",
    )
    for key in candidate_keys:
        rows = projected.get(key)
        if not isinstance(rows, list):
            continue
        for item in rows:
            if not isinstance(item, dict):
                continue
            target_board = _safe_int(item.get("target_board"), 1)
            date_key = "second_board_trade_date" if target_board >= 2 else "first_board_trade_date"
            item_trade_date = (
                _parse_iso_trade_date(item.get("latest_as_of"))
                or _parse_iso_trade_date(projected.get(date_key))
                or _parse_iso_trade_date(projected.get("trade_date"))
            )
            if item_trade_date is None:
                continue
            clock_day, session_name = _resolve_signal_clock(snapshot_clock, item_trade_date)
            item.update(
                _resolve_promotion_signal_status(
                    target_board=target_board,
                    trade_date=item_trade_date,
                    latest_as_of=str(item.get("latest_as_of") or ""),
                    kline_context=item.get("kline_confirmation") or {},
                    today=clock_day,
                    session_name=session_name,
                )
            )
    projected.pop("_cache_capacity", None)
    if not compact:
        return projected
    projected["compact"] = True
    projected.pop("actual_limit_up_replay", None)
    projected.pop("second_board_candidates", None)
    debug = dict(projected.get("debug") or {})
    debug.pop("mixed_ranked_candidates", None)
    projected["debug"] = debug
    return projected


def _is_completed_promotion_outcome_date(
    trade_date_value: date | None,
    *,
    now: datetime | None = None,
) -> bool:
    """只允许已经完成收盘处理的交易日进入晋级学习。

    涨停池盘中会持续写入，不能仅凭“出现了下一交易日”就把预测永久结算。
    当天统一等到 15:10 以后；历史日期天然视为完成。
    """
    if trade_date_value is None:
        return False
    if trade_date_value.weekday() >= 5 or is_official_closed_day(trade_date_value):
        return False
    current = now or datetime.now()
    if trade_date_value < current.date():
        return True
    if trade_date_value > current.date():
        return False
    return (current.hour, current.minute) >= (15, 10)


def _parse_iso_trade_date(value: str | None) -> date | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return date.fromisoformat(raw[:10])
        except ValueError:
            return None


def _parse_iso_datetime(value: str | None) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


async def _trade_day_gap(db: AsyncSession, start_date: date, end_date: date) -> int:
    if end_date <= start_date:
        return 0
    observed_count = (
        await db.execute(
            select(func.count(func.distinct(StockKline.trade_date))).where(
                StockKline.trade_date > start_date,
                StockKline.trade_date <= end_date,
            )
        )
    ).scalar_one()
    if observed_count:
        return _safe_int(observed_count)
    try:
        days = await trade_calendar.trade_days_between(start_date + timedelta(days=1), end_date)
        return len(days)
    except Exception:
        return _weekday_gap(start_date, end_date)


def _resolve_signal_clock(latest_as_of: str | None, fallback_trade_date: date) -> tuple[date, str | None]:
    as_of_dt = _parse_iso_datetime(latest_as_of)
    if as_of_dt is None:
        return _parse_iso_trade_date(latest_as_of) or fallback_trade_date, None
    return as_of_dt.date(), trade_calendar.get_trade_session(as_of_dt)


def _resolve_snapshot_clock_source(snapshot: dict, rows: list[dict]) -> str:
    snapshot_time = str((snapshot or {}).get("snapshot_time") or "").strip()
    if snapshot_time:
        return snapshot_time
    for row in rows:
        detail = row.get("detail") or {}
        raw = str(row.get("latest_as_of") or detail.get("as_of") or "").strip()
        if raw:
            return raw
    return ""


async def _resolve_first_board_trade_date(db: AsyncSession, snapshot: dict) -> date:
    snapshot_trade_date = _parse_iso_trade_date((snapshot or {}).get("trade_date"))
    snapshot_time = _parse_iso_datetime((snapshot or {}).get("snapshot_time"))
    # 09:25/09:35 预热时，牛股雷达快照里的 trade_date 仍是上一收盘日，
    # 但 auction_data 已经属于当前交易日。旧逻辑因此一直读取昨天的竞价，
    # 当日首板确认既不进榜也不落库。只有当前日确有09:30前竞价数据时才
    # 前移锚点，周末、停更或普通收盘快照仍严格沿用原交易日。
    if (
        snapshot_trade_date
        and snapshot_time is not None
        and snapshot_time.date() > snapshot_trade_date
    ):
        auction_count = (
            await db.execute(
                select(func.count(AuctionData.id)).where(
                    AuctionData.trade_date == snapshot_time.date(),
                    AuctionData.auction_time.between("09:15:00", "09:25:30"),
                )
            )
        ).scalar_one()
        if _safe_int(auction_count) > 0:
            return snapshot_time.date()
    if snapshot_trade_date:
        return snapshot_trade_date
    return await resolve_latest_trade_date(db, SectorPersistence.trade_date)


def _resolve_promotion_signal_status(
    *,
    target_board: int,
    trade_date: date,
    latest_as_of: str | None = None,
    kline_context: dict | None = None,
    today: date | None = None,
    session_name: str | None = None,
) -> dict[str, str]:
    current_day = today or date.today()
    current_session = session_name or trade_calendar.get_trade_session()
    as_of_trade_date = _parse_iso_trade_date(latest_as_of)
    uses_provisional_bar = bool((kline_context or {}).get("provisional_last_bar"))
    is_current_day_signal = trade_date == current_day or as_of_trade_date == current_day
    is_intraday_session = current_session in {
        "pre_auction",
        "morning",
        "lunch_break",
        "afternoon",
    }

    if target_board >= 2:
        if is_current_day_signal and is_intraday_session:
            status = PROMOTION_SIGNAL_STATUS_INTRADAY_PREVIEW
            reason = "首板仍在盘中，封板质量、炸板次数和收盘换手尚未最终确认"
        else:
            status = PROMOTION_SIGNAL_STATUS_CLOSE_CONFIRMED
            reason = "基于已收盘首板池评估次日晋级"
        return {
            "signal_status": status,
            "signal_status_label": PROMOTION_SIGNAL_STATUS_LABELS[status],
            "signal_status_reason": reason,
        }

    if is_current_day_signal and is_intraday_session:
        status = PROMOTION_SIGNAL_STATUS_INTRADAY_PREVIEW
        reason = (
            "当日异动盘中触发，且最新K线未收盘，按预判降权"
            if uses_provisional_bar
            else "当日异动盘中触发，K线确认基于最近已完成日线"
        )
    elif uses_provisional_bar:
        status = PROMOTION_SIGNAL_STATUS_INTRADAY_PREVIEW
        reason = "最新K线仍为盘中临时日线，先按预判处理"
    else:
        status = PROMOTION_SIGNAL_STATUS_CLOSE_CONFIRMED
        reason = "历史K线与异动信号已按收盘口径确认"

    return {
        "signal_status": status,
        "signal_status_label": PROMOTION_SIGNAL_STATUS_LABELS[status],
        "signal_status_reason": reason,
    }


def _promotion_learning_bucket(target_board: int, candidate_route: str | None) -> str:
    route = str(candidate_route or "unknown").strip() or "unknown"
    return f"T{target_board}:{route}"


def _promotion_context_learning_bucket(base_bucket: str, factors: dict | None = None) -> str:
    """按当时可见的市场/消息状态细分校准桶，样本不足时仍回退基础路线。"""
    normalized_base = str(base_bucket or "").split("|state:", 1)[0]
    factors = factors or {}
    if not normalized_base.startswith("T1:"):
        return normalized_base
    route = normalized_base.split(":", 1)[-1]
    if route == "news_catalyst_start":
        mapping_mode = str(factors.get("news_mapping_mode") or "")
        if mapping_mode in {"direct_code", "sector_inferred"}:
            return f"{normalized_base}|state:{mapping_mode}"
    if bool(factors.get("low_base_sector_ignition_ready")):
        state = (
            "low_base_industry_funding"
            if bool(factors.get("low_base_sector_ignition_confirmed"))
            else "low_base_industry"
        )
        return f"{normalized_base}|state:{state}"
    if bool(factors.get("market_broad_first_board_overflow")) and bool(
        factors.get("broad_rotation_member_setup")
    ):
        return f"{normalized_base}|state:broad_rotation"
    market_risk_level = str(factors.get("market_risk_level") or "")
    if market_risk_level in {"weak", "hostile"}:
        return f"{normalized_base}|state:weak_market"
    return f"{normalized_base}|state:normal_market"


def _limit_up_threshold_for_code(
    code: str,
    name: str = "",
    trade_date_value: date | str | None = None,
) -> float:
    """Backward-compatible wrapper around the central date-effective rule."""

    return limit_up_change_threshold(code, name, trade_date_value)


def _is_limit_up_change(
    code: str,
    name: str,
    change_pct: float,
    trade_date_value: date | str | None = None,
) -> bool:
    return is_limit_up_change(
        code,
        _safe_float(change_pct),
        name=name,
        trade_date=trade_date_value,
    )


def _build_prediction_reason_snapshot(item: dict) -> dict:
    factors = item.get("probability_factors") or {}
    return {
        "signal_summary": str(item.get("signal_summary") or ""),
        "primary_reason": str(item.get("primary_reason") or ""),
        "secondary_reason": str(item.get("secondary_reason") or ""),
        "sector_name": str(item.get("sector_name") or ""),
        "candidate_route": str(item.get("candidate_route") or ""),
        "candidate_route_label": str(item.get("candidate_route_label") or ""),
        "strategy_lane": str(item.get("strategy_lane") or ""),
        "strategy_lane_label": str(item.get("strategy_lane_label") or ""),
        "setup_grade": str(item.get("setup_grade") or ""),
        "time_horizon": str(item.get("time_horizon") or ""),
        "prediction_actionable": bool(
            item.get("prediction_actionable")
            or factors.get("prediction_actionable")
        ),
        "prediction_trade_gate_passed": bool(
            item.get("prediction_trade_gate_passed")
            or factors.get("prediction_trade_gate_passed")
        ),
        "trade_actionability_status": str(
            item.get("trade_actionability_status")
            or factors.get("trade_actionability_status")
            or ""
        ),
        "trade_actionability_label": str(
            item.get("trade_actionability_label")
            or factors.get("trade_actionability_label")
            or ""
        ),
        "strict_blockers": list(item.get("strict_blockers") or []),
        "risk_flags": item.get("risk_flags") or [],
        "news_evidence": deepcopy(factors.get("news_evidence")) if isinstance(factors.get("news_evidence"), dict) else {},
    }


def _explain_failed_promotion(record: PromotionPredictionRecord, bars: list[StockKline]) -> tuple[str, list[str]]:
    factors = _json_loads_safe(record.factors_json)
    reason_snapshot = _json_loads_safe(record.reason_snapshot)
    tags: list[str] = []

    max_change = max((_safe_float(bar.change_pct) for bar in bars), default=0.0)
    last_change = _safe_float(bars[-1].change_pct) if bars else 0.0
    max_bar = max(bars, key=lambda bar: _safe_float(bar.change_pct), default=None)
    high_change = 0.0
    if max_bar and _safe_float(max_bar.prev_close) > 0 and _safe_float(max_bar.high) > 0:
        high_change = _safe_percent_change(_safe_float(max_bar.high), _safe_float(max_bar.prev_close))

    if high_change >= _limit_up_threshold_for_code(
        record.code,
        record.name or "",
        getattr(max_bar, "trade_date", None),
    ) - 1.0 and last_change < 5.0:
        tags.append("冲板回落")
    if max_change < 3.0:
        tags.append("点火不足")

    break_count = _safe_int(factors.get("break_count"))
    distribution_penalty = _safe_float(factors.get("distribution_penalty"))
    if break_count >= 1 or distribution_penalty >= 0.08:
        tags.append("封板/筹码松动")

    sector_score = _safe_float(factors.get("sector_continuity_score"))
    if 0 < sector_score < 0.35:
        tags.append("板块持续性不足")
    if bool(factors.get("market_weak_follow_through")):
        tags.append("市场接力弱")

    route = str(record.candidate_route or "")
    if record.target_board == 1:
        kline_score = _safe_float(factors.get("kline_confirmation_score"))
        if 0 < kline_score < 0.55:
            tags.append("K线确认不够")
        if not bool(factors.get("strict_ready")) and route != "second_board_promotion":
            tags.append("冲刺条件未硬化")

    if not tags:
        tags.append("未形成涨停确认")

    route_label = str(reason_snapshot.get("candidate_route_label") or record.candidate_route or "当前路由")
    if record.target_board == 2:
        reason = (
            f"{route_label}未晋级：{ '、'.join(tags[:3]) }。"
            f"预测后最高涨幅{max_change:.1f}%，收盘涨幅{last_change:.1f}%。"
        )
    else:
        reason = (
            f"{route_label}未首板：{ '、'.join(tags[:3]) }。"
            f"{record.horizon_days}日窗口内最高涨幅{max_change:.1f}%，收盘涨幅{last_change:.1f}%。"
        )
    return reason, tags


def _bar_intraday_max_change_pct(bar: StockKline | dict) -> float:
    """Return the observable intraday high versus that session's previous close."""
    getter = bar.get if isinstance(bar, dict) else lambda key, default=None: getattr(bar, key, default)
    high = _safe_float(getter("high"))
    prev_close = _safe_float(getter("prev_close"))
    if high > 0 and prev_close > 0:
        return (high / prev_close - 1.0) * 100.0
    return _safe_float(getter("change_pct"))


async def _evaluate_promotion_prediction_record(
    db: AsyncSession,
    record: PromotionPredictionRecord,
    *,
    now: datetime | None = None,
) -> bool:
    if record.outcome_status != "pending" or not record.prediction_trade_date:
        return False

    horizon = max(1, _safe_int(record.horizon_days, 1))
    # StockKline is the authoritative observed-session clock.  Limit-up source
    # rows must never invent a market date (for example a stale holiday cache).
    market_dates = [
        row[0]
        for row in (
            await db.execute(
                select(StockKline.trade_date)
                .where(StockKline.trade_date > record.prediction_trade_date)
                .distinct()
                .order_by(StockKline.trade_date)
                .limit(max(horizon + 20, 40))
            )
        ).all()
        if row[0] is not None
    ]
    market_dates = [
        trade_date_value
        for trade_date_value in sorted(set(market_dates))
        if _is_completed_promotion_outcome_date(trade_date_value, now=now)
    ][:horizon]
    if len(market_dates) < horizon:
        return False
    outcome_date = market_dates[horizon - 1]

    bars_result = await db.execute(
        select(StockKline)
        .where(
            StockKline.code == record.code,
            StockKline.trade_date > record.prediction_trade_date,
            StockKline.trade_date <= outcome_date,
        )
        .order_by(StockKline.trade_date)
    )
    bars = list(bars_result.scalars().all())
    limit_up_rows = (
        await db.execute(
            select(
                LimitUpPool.trade_date,
                LimitUpPool.consecutive_days,
            ).where(
                LimitUpPool.code == record.code,
                LimitUpPool.trade_date >= record.prediction_trade_date,
                LimitUpPool.trade_date <= outcome_date,
            )
        )
    ).all()
    limit_up_board_by_date: dict[date, int] = {}
    allowed_limit_up_dates = set(market_dates) | {record.prediction_trade_date}
    for trade_date_value, consecutive_days in limit_up_rows:
        if trade_date_value is None or trade_date_value not in allowed_limit_up_dates:
            continue
        limit_up_board_by_date[trade_date_value] = max(
            limit_up_board_by_date.get(trade_date_value, 0),
            max(_safe_int(consecutive_days, 1), 1),
        )

    success = False
    actual_limit_up_date = None
    if record.target_board == 2:
        next_trade_date = market_dates[0]
        previous_board = limit_up_board_by_date.get(record.prediction_trade_date, 0)
        next_board = limit_up_board_by_date.get(next_trade_date, 0)
        # 部分数据源会把连续两天涨停都写成 consecutive_days=1。只有前一日
        # 确为首板且次日仍涨停时才纠正为二板，不能把普通次日首板算作晋级。
        normalized_next_board = (
            2
            if previous_board == 1 and next_board == 1
            else next_board
        )
        success = normalized_next_board == 2
        actual_limit_up_date = next_trade_date if success else None
        outcome_date = next_trade_date
        bars = bars[:1]
    else:
        prediction_day_limit_up = (
            limit_up_board_by_date.get(record.prediction_trade_date, 0) > 0
        )
        first_board_dates = {
            trade_date_value
            for trade_date_value, board_height in limit_up_board_by_date.items()
            if trade_date_value > record.prediction_trade_date
            and board_height == 1
        }
        # A T1 label is a new first-board start.  If the stock was already in
        # the prediction-day limit-up pool, a next-session hit is continuation
        # (T2+) even when the vendor leaves consecutive_days at 1.
        success = bool(
            promotion_event_label(
                target_board=1,
                outcome_limit_up=bool(first_board_dates),
                prediction_day_limit_up=prediction_day_limit_up,
            )
        )
        actual_limit_up_date = min(first_board_dates) if success else None

    max_change = max((_bar_intraday_max_change_pct(bar) for bar in bars), default=0.0)
    close_change = _safe_float(bars[-1].change_pct) if bars else 0.0
    record.outcome_status = "success" if success else "failed"
    record.outcome_trade_date = outcome_date
    record.actual_limit_up_date = actual_limit_up_date
    record.actual_max_change_pct = max_change
    record.actual_close_change_pct = close_change
    if success:
        record.failure_reason = ""
        record.failure_tags_json = "[]"
    else:
        reason, tags = _explain_failed_promotion(record, bars)
        record.failure_reason = reason
        record.failure_tags_json = _json_dumps_safe(tags)
    return True


async def _evaluate_promotion_prediction_records_bulk(
    db: AsyncSession,
    records: list[PromotionPredictionRecord],
    *,
    now: datetime | None = None,
) -> int:
    """批量结算正式预测池，避免逐股N+1查询并补齐未命中的池内负样本。"""
    pending = [
        record
        for record in records
        if record.outcome_status == "pending" and record.prediction_trade_date
    ]
    if not pending:
        return 0
    current = now or datetime.now()
    min_prediction_date = min(record.prediction_trade_date for record in pending)
    kline_date_rows = (
        await db.execute(
            select(StockKline.trade_date)
            .where(StockKline.trade_date > min_prediction_date)
            .distinct()
            .order_by(StockKline.trade_date)
        )
    ).all()
    # The K-line calendar is authoritative.  Never union vendor limit-pool
    # dates into the session clock because stale holiday payloads corrupt labels.
    completed_market_dates = [
        trade_date_value
        for trade_date_value in sorted({
            row[0]
            for row in kline_date_rows
            if row[0] is not None
        })
        if _is_completed_promotion_outcome_date(trade_date_value, now=current)
    ]
    outcome_dates: dict[int, date] = {}
    for record in pending:
        horizon = max(1, _safe_int(record.horizon_days, 1))
        future_dates = [
            trade_date_value
            for trade_date_value in completed_market_dates
            if trade_date_value > record.prediction_trade_date
        ][:horizon]
        if len(future_dates) >= horizon:
            outcome_dates[record.id] = future_dates[-1]
    evaluable = [record for record in pending if record.id in outcome_dates]
    if not evaluable:
        return 0

    codes = sorted({str(record.code or "").strip() for record in evaluable if str(record.code or "").strip()})
    max_outcome_date = max(outcome_dates[record.id] for record in evaluable)
    bars_by_code: dict[str, list[StockKline]] = defaultdict(list)
    limit_up_dates_by_code: dict[str, set[date]] = defaultdict(set)
    limit_up_board_by_code: dict[str, dict[date, int]] = defaultdict(dict)
    observed_market_date_set = set(completed_market_dates) | {
        record.prediction_trade_date for record in evaluable
    }
    # SQLite常见变量上限为999，按800只切片，同时避免一次加载全市场历史。
    for offset in range(0, len(codes), 800):
        code_chunk = codes[offset : offset + 800]
        bars_result = await db.execute(
            select(StockKline).where(
                StockKline.code.in_(code_chunk),
                StockKline.trade_date > min_prediction_date,
                StockKline.trade_date <= max_outcome_date,
            )
        )
        for bar in bars_result.scalars().all():
            bars_by_code[str(bar.code or "").strip()].append(bar)
        limit_result = await db.execute(
            select(
                LimitUpPool.code,
                LimitUpPool.trade_date,
                LimitUpPool.consecutive_days,
            ).where(
                LimitUpPool.code.in_(code_chunk),
                LimitUpPool.trade_date >= min_prediction_date,
                LimitUpPool.trade_date <= max_outcome_date,
            )
        )
        for code, trade_date_value, consecutive_days in limit_result.all():
            normalized_code = str(code or "").strip()
            if (
                normalized_code
                and trade_date_value is not None
                and trade_date_value in observed_market_date_set
            ):
                limit_up_dates_by_code[normalized_code].add(trade_date_value)
                limit_up_board_by_code[normalized_code][trade_date_value] = max(
                    limit_up_board_by_code[normalized_code].get(trade_date_value, 0),
                    max(_safe_int(consecutive_days, 1), 1),
                )

    changed = 0
    for record in evaluable:
        code = str(record.code or "").strip()
        outcome_date = outcome_dates[record.id]
        bars = sorted(
            [
                bar
                for bar in bars_by_code.get(code, [])
                if record.prediction_trade_date < bar.trade_date <= outcome_date
            ],
            key=lambda bar: bar.trade_date,
        )
        limit_up_dates = {
            trade_date_value
            for trade_date_value in limit_up_dates_by_code.get(code, set())
            if record.prediction_trade_date < trade_date_value <= outcome_date
        }
        if _safe_int(record.target_board) == 2:
            next_trade_date = next(
                trade_date_value
                for trade_date_value in completed_market_dates
                if trade_date_value > record.prediction_trade_date
            )
            board_by_date = limit_up_board_by_code.get(code, {})
            previous_board = board_by_date.get(record.prediction_trade_date, 0)
            next_board = board_by_date.get(next_trade_date, 0)
            normalized_next_board = (
                2
                if previous_board == 1 and next_board == 1
                else next_board
            )
            success = normalized_next_board == 2
            actual_limit_up_date = next_trade_date if success else None
            outcome_date = next_trade_date
            bars = bars[:1]
        else:
            board_by_date = limit_up_board_by_code.get(code, {})
            prediction_day_limit_up = (
                board_by_date.get(record.prediction_trade_date, 0) > 0
            )
            first_board_dates = {
                trade_date_value
                for trade_date_value in limit_up_dates
                if board_by_date.get(trade_date_value, 0) == 1
            }
            success = bool(
                promotion_event_label(
                    target_board=1,
                    outcome_limit_up=bool(first_board_dates),
                    prediction_day_limit_up=prediction_day_limit_up,
                )
            )
            actual_limit_up_date = min(first_board_dates) if success else None
        max_change = max((_bar_intraday_max_change_pct(bar) for bar in bars), default=0.0)
        close_change = _safe_float(bars[-1].change_pct) if bars else 0.0
        record.outcome_status = "success" if success else "failed"
        record.outcome_trade_date = outcome_date
        record.actual_limit_up_date = actual_limit_up_date
        record.actual_max_change_pct = max_change
        record.actual_close_change_pct = close_change
        if success:
            record.failure_reason = ""
            record.failure_tags_json = "[]"
        else:
            reason, tags = _explain_failed_promotion(record, bars)
            record.failure_reason = reason
            record.failure_tags_json = _json_dumps_safe(tags)
        changed += 1
    return changed


async def _reset_premature_promotion_outcomes(
    db: AsyncSession,
    *,
    now: datetime | None = None,
) -> int:
    """自愈盘中被旧逻辑提前结算的当天样本。"""
    current = now or datetime.now()
    if _is_completed_promotion_outcome_date(current.date(), now=current):
        return 0
    result = await db.execute(
        select(PromotionPredictionRecord).where(
            PromotionPredictionRecord.outcome_status.in_(["success", "failed"]),
            PromotionPredictionRecord.outcome_trade_date == current.date(),
        )
    )
    records = list(result.scalars().all())
    for record in records:
        record.outcome_status = "pending"
        record.outcome_trade_date = None
        record.actual_limit_up_date = None
        record.actual_max_change_pct = None
        record.actual_close_change_pct = None
        record.failure_reason = ""
        record.failure_tags_json = "[]"
    if records:
        await db.flush()
    return len(records)


async def _refresh_promotion_learning(
    db: AsyncSession,
    *,
    now: datetime | None = None,
) -> int:
    await _ensure_promotion_learning_storage(db)
    changed = await _reset_premature_promotion_outcomes(db, now=now)
    result = await db.execute(
        select(PromotionPredictionRecord)
        .where(PromotionPredictionRecord.outcome_status == "pending")
        .order_by(desc(PromotionPredictionRecord.prediction_trade_date), desc(PromotionPredictionRecord.id))
        .limit(5000)
    )
    records = []
    for record in _latest_learning_batch_records(list(result.scalars().all())):
        factors = _json_loads_safe(record.factors_json)
        raw_source = str(
            getattr(record, "snapshot_source", "")
            or factors.get("prediction_snapshot_source")
            or ""
        ).strip()
        is_legacy_ranked_record = (
            raw_source in {"", "legacy"}
            and factors.get("prediction_ranked_selected", True) is not False
        )
        if _prediction_record_snapshot_source(record, factors) == "schedule" or is_legacy_ranked_record:
            records.append(record)
    changed += await _evaluate_promotion_prediction_records_bulk(db, records, now=now)
    if changed:
        await db.flush()
    return changed


def _select_versioned_learning_records(
    records: list[PromotionPredictionRecord],
    *,
    anchor_date: date,
) -> tuple[list[PromotionPredictionRecord], dict]:
    """Prefer current-model, recent samples without letting legacy rows dominate."""
    recent_cutoff = anchor_date - timedelta(days=PROMOTION_LEARNING_RECENT_DAYS)
    recent_records = [
        record
        for record in records
        if record.prediction_trade_date and record.prediction_trade_date >= recent_cutoff
    ]
    source_records = recent_records if len(recent_records) >= 4 else list(records)
    ordered = sorted(
        source_records,
        key=lambda record: (
            record.prediction_trade_date or date.min,
            _prediction_record_snapshot_time(record),
            _safe_int(record.id),
        ),
        reverse=True,
    )
    current_model_records = [
        record
        for record in ordered
        if _prediction_record_model_version(record) == PROMOTION_MODEL_VERSION
    ]
    legacy_records = [
        record
        for record in ordered
        if _prediction_record_model_version(record) != PROMOTION_MODEL_VERSION
    ]
    if len(current_model_records) >= 8:
        selected = current_model_records
        mode = "current_model_only"
    elif current_model_records:
        legacy_fill = max(4, 8 - len(current_model_records))
        selected = current_model_records + legacy_records[:legacy_fill]
        mode = "current_model_with_capped_prior"
    else:
        selected = legacy_records[:PROMOTION_LEGACY_SAMPLE_CAP]
        mode = "capped_legacy_prior"
    selected.sort(
        key=lambda record: (
            record.prediction_trade_date or date.min,
            _prediction_record_snapshot_time(record),
            _safe_int(record.id),
        )
    )
    return selected, {
        "learning_version_mode": mode,
        "raw_sample_count": len(records),
        "recent_sample_count": len(recent_records),
        "current_model_sample_count": len(current_model_records),
        "legacy_sample_count_used": sum(
            1
            for record in selected
            if _prediction_record_model_version(record) != PROMOTION_MODEL_VERSION
        ),
        "recent_cutoff": str(recent_cutoff),
    }


def _promotion_record_uses_current_label_contract(
    record: PromotionPredictionRecord,
    factors: dict,
) -> bool:
    label_version = str(factors.get("promotion_event_label_version") or "").strip()
    if label_version:
        return label_version == PROMOTION_LABEL_VERSION
    # Test fixtures and the first governed run may omit the explicit factor but
    # still carry the newly bumped model identity. Older model identities are
    # incompatible because continuation boards could have been counted as T1.
    return _prediction_record_model_version(record, factors) == PROMOTION_MODEL_VERSION


async def _load_promotion_learning_stats(db: AsyncSession) -> dict[str, dict]:
    await _ensure_promotion_learning_storage(db)
    cutoff = date.today() - timedelta(days=180)
    result = await db.execute(
        select(PromotionPredictionRecord)
        .where(
            PromotionPredictionRecord.outcome_status.in_(["success", "failed"]),
            PromotionPredictionRecord.prediction_trade_date >= cutoff,
        )
    )
    latest_records = _latest_learning_batch_records(list(result.scalars().all()))
    learning_anchor_date = max(
        (record.prediction_trade_date for record in latest_records if record.prediction_trade_date),
        default=date.today(),
    )
    canonical_bucket_presence: set[str] = set()
    for record in latest_records:
        factors = _json_loads_safe(record.factors_json)
        if not _promotion_record_uses_current_label_contract(record, factors):
            continue
        if _prediction_record_snapshot_source(record, factors) == "schedule":
            canonical_bucket_presence.add(
                _promotion_record_learning_bucket(record, factors).split("|state:", 1)[0]
            )

    grouped: dict[str, list[PromotionPredictionRecord]] = {}
    pool_grouped: dict[str, list[PromotionPredictionRecord]] = {}
    for record in latest_records:
        factors = _json_loads_safe(record.factors_json)
        if _is_legacy_observation_first_board_record(record):
            continue
        if not _promotion_record_uses_current_label_contract(record, factors):
            # Old outcomes may encode a next-day continuation as a T1 success.
            # Never mix that incompatible truth into the governed v2 calibrator.
            continue
        bucket = _promotion_record_learning_bucket(record, factors).split("|state:", 1)[0]
        context_bucket = _promotion_context_learning_bucket(bucket, factors)
        source = _prediction_record_snapshot_source(record, factors)
        # 一旦某路由已有正式 schedule 样本，就不再把早期页面临时记录混入校准。
        if bucket in canonical_bucket_presence and source != "schedule":
            continue
        for learning_key in {bucket, context_bucket}:
            pool_grouped.setdefault(learning_key, []).append(record)
        is_learning_eligible = (
            factors.get("learning_eligible") is True
            or (
                "learning_eligible" not in factors
                and factors.get("prediction_ranked_selected", True) is not False
            )
        )
        if not is_learning_eligible:
            continue
        for learning_key in {bucket, context_bucket}:
            grouped.setdefault(learning_key, []).append(record)

    # Direction labels are independently verified T+1 close returns. Legacy
    # outcome_status/actual_close_change_pct belong to the limit-up contract and
    # can contain zero-filled missing bars; they are never direction evidence.
    direction_changes: dict[int, float] = {}
    if latest_records:
        current = datetime.now()
        calendar = dict((await db.execute(select(
            TradeCalendarModel.trade_date, TradeCalendarModel.is_trade_day
        ).where(TradeCalendarModel.trade_date >= min(
            record.prediction_trade_date for record in latest_records
        ), TradeCalendarModel.trade_date <= current.date()))).all())
        pairs = {}
        for record in latest_records:
            factors = _json_loads_safe(record.factors_json)
            if (_prediction_record_snapshot_source(record, factors) != "schedule"
                    or _prediction_record_snapshot_context(record, factors) not in PROMOTION_CANONICAL_CLOSE_CONTEXTS):
                continue
            outcome_day, error = next_recorded_trade_day(
                record.prediction_trade_date, calendar, through=current.date()
            )
            if not error and _is_completed_promotion_outcome_date(outcome_day, now=current):
                pairs[record.id] = outcome_day
        dates = {record.prediction_trade_date for record in latest_records} | set(pairs.values())
        codes = sorted({record.code for record in latest_records})
        bars = {}
        for offset in range(0, len(codes), 700):
            rows = (await db.execute(select(StockKline).where(
                StockKline.code.in_(codes[offset:offset + 700]),
                StockKline.trade_date >= min(dates),
                StockKline.trade_date <= max(dates),
            ))).scalars().all()
            bars.update({(bar.code, bar.trade_date): bar for bar in rows})
        for record in latest_records:
            after = bars.get((record.code, pairs.get(record.id)))
            before = bars.get((record.code, record.prediction_trade_date))
            if (record.id in pairs and not formal_outcome_bar_error(before, after)
                    and isinstance(after.change_pct, (int, float)) and isfinite(after.change_pct)):
                direction_changes[record.id] = (after.close / after.prev_close - 1) * 100

    stats: dict[str, dict] = {}
    for bucket, raw_records in grouped.items():
        records, version_meta = _select_versioned_learning_records(
            raw_records,
            anchor_date=learning_anchor_date,
        )
        sample_count = len(records)
        if sample_count < 4:
            continue
        success_count = sum(1 for item in records if item.outcome_status == "success")
        success_rate = success_count / sample_count
        directional_returns = [direction_changes[item.id] for item in records if item.id in direction_changes]
        directional_sample_count = len(directional_returns)
        rising_count = sum(value > 0 for value in directional_returns)
        strong_rising_count = sum(1 for item in records if _safe_float(item.actual_max_change_pct) >= 5.0)
        avg_max_change_pct = sum(_safe_float(item.actual_max_change_pct) for item in records) / sample_count
        avg_close_change_pct = sum(directional_returns) / directional_sample_count if directional_sample_count else None
        avg_predicted = sum(_safe_float(item.calibrated_probability, _safe_float(item.predicted_probability)) for item in records) / sample_count
        target_board = _safe_int(records[0].target_board, 1)
        # 首板是低基准率事件，用5%弱信息先验；二板用20%先验。
        # 后验概率避免小样本直接归零，同时在样本充分后服从真实命中率。
        prior_alpha, prior_beta = ((1.0, 19.0) if target_board == 1 else (2.0, 8.0))
        empirical_probability = (
            success_count + prior_alpha
        ) / (
            sample_count + prior_alpha + prior_beta
        )
        # “上涨”与“涨停”是不同标签。上涨使用50%的中性先验，强上涨使用
        # 10%的弱信息先验，避免把低基准率的首板后验误当成方向概率。
        directional_prior_alpha, directional_prior_beta = 10.0, 10.0
        strong_prior_alpha, strong_prior_beta = 2.0, 18.0
        empirical_rising_probability = (
            rising_count + directional_prior_alpha
        ) / (
            directional_sample_count + directional_prior_alpha + directional_prior_beta
        )
        empirical_strong_rising_probability = (
            strong_rising_count + strong_prior_alpha
        ) / (
            sample_count + strong_prior_alpha + strong_prior_beta
        )
        adjustment = empirical_probability - avg_predicted
        brier_score = sum(
            (
                _safe_float(item.calibrated_probability, _safe_float(item.predicted_probability))
                - (1.0 if item.outcome_status == "success" else 0.0)
            ) ** 2
            for item in records
        ) / sample_count
        raw_pool_records = pool_grouped.get(bucket) or raw_records
        pool_records, pool_version_meta = _select_versioned_learning_records(
            raw_pool_records,
            anchor_date=learning_anchor_date,
        )
        pool_success_count = sum(1 for item in pool_records if item.outcome_status == "success")
        pool_directional_returns = [direction_changes[item.id] for item in pool_records if item.id in direction_changes]
        pool_directional_sample_count = len(pool_directional_returns)
        pool_rising_count = sum(value > 0 for value in pool_directional_returns)
        pool_strong_rising_count = sum(1 for item in pool_records if _safe_float(item.actual_max_change_pct) >= 5.0)
        missed_success_count = max(pool_success_count - success_count, 0)
        selection_recall = success_count / pool_success_count if pool_success_count else 0.0
        # 全池口径的上涨/强涨先验，使用与主榜样本同样的 beta-binomial 平滑，
        # 但把所有同 bucket 的 ranked+pool_unranked 都纳入；这样当主榜样本
        # 还很少时也能拿到稳定的上涨概率，反哺 next_day_rise 显示。
        pool_size = len(pool_records) or 1
        pool_empirical_rising_probability = (
            pool_rising_count + directional_prior_alpha
        ) / (
            pool_directional_sample_count + directional_prior_alpha + directional_prior_beta
        )
        pool_empirical_strong_rising_probability = (
            pool_strong_rising_count + strong_prior_alpha
        ) / (
            pool_size + strong_prior_alpha + strong_prior_beta
        )
        stats[bucket] = {
            "sample_count": sample_count,
            **version_meta,
            "pool_raw_sample_count": _safe_int(pool_version_meta.get("raw_sample_count")),
            "pool_current_model_sample_count": _safe_int(pool_version_meta.get("current_model_sample_count")),
            "model_version": PROMOTION_MODEL_VERSION,
            "success_count": success_count,
            "success_rate": round(success_rate, 3),
            "directional_sample_count": directional_sample_count,
            "directional_unknown_count": sample_count - directional_sample_count,
            "directional_coverage": round(directional_sample_count / sample_count, 4),
            "directional_evidence_status": "complete" if directional_sample_count == sample_count else "partial" if directional_sample_count else "unavailable",
            "rising_rate": round(rising_count / directional_sample_count, 3) if directional_sample_count else None,
            "strong_rising_rate": round(strong_rising_count / sample_count, 3),
            "avg_max_change_pct": round(avg_max_change_pct, 3),
            "avg_close_change_pct": round(avg_close_change_pct, 3) if avg_close_change_pct is not None else None,
            "avg_predicted_probability": round(avg_predicted, 3),
            "empirical_probability": round(empirical_probability, 4),
            "empirical_rising_probability": round(empirical_rising_probability, 4),
            "empirical_strong_rising_probability": round(
                empirical_strong_rising_probability,
                4,
            ),
            "prior_alpha": prior_alpha,
            "prior_beta": prior_beta,
            "model_adjustment": round(adjustment, 3),
            "brier_score": round(brier_score, 4),
            "calibration_bias": round(avg_predicted - success_rate, 4),
            "pool_sample_count": len(pool_records),
            "pool_success_count": pool_success_count,
            "pool_failure_count": max(len(pool_records) - pool_success_count, 0),
            "pool_directional_sample_count": pool_directional_sample_count,
            "pool_directional_unknown_count": len(pool_records) - pool_directional_sample_count,
            "pool_directional_coverage": round(pool_directional_sample_count / len(pool_records), 4) if pool_records else 0.0,
            "pool_rising_count": pool_rising_count,
            "pool_strong_rising_count": pool_strong_rising_count,
            "pool_empirical_rising_probability": round(pool_empirical_rising_probability, 4),
            "pool_empirical_strong_rising_probability": round(pool_empirical_strong_rising_probability, 4),
            "missed_success_count": missed_success_count,
            "selection_recall": round(selection_recall, 4),
        }
    return stats


def _promotion_record_learning_bucket(
    record: PromotionPredictionRecord,
    factors: dict | None = None,
) -> str:
    factors = factors or _json_loads_safe(record.factors_json)
    if _safe_int(record.target_board) == 2:
        second_board_route = str(factors.get("second_board_route") or "").strip()
        if second_board_route:
            return f"T2:{second_board_route}"
    return str(record.learning_bucket or "").strip() or _promotion_learning_bucket(
        record.target_board,
        record.candidate_route,
    )


def _prediction_record_key(item: dict) -> tuple[str, int, str]:
    return (
        str(item.get("code") or "").strip(),
        _safe_int(item.get("target_board"), 1),
        str(item.get("candidate_route") or "").strip(),
    )


def _is_legacy_observation_first_board_record(record: PromotionPredictionRecord) -> bool:
    if _safe_int(record.target_board) != 1:
        return False
    route = str(record.candidate_route or "").strip()
    return route in FIRST_BOARD_LEGACY_OBSERVATION_RECORD_ROUTES


def _is_recordable_prediction_candidate(item: dict) -> bool:
    target_board = _safe_int(item.get("target_board"), 1)
    if target_board != 1:
        # 二板完整池必须落库，未通过闸门的样本以 pool_unranked 保存，才能
        # 正确计算召回率和漏选原因；只有 relay_pool_ready 会进入正式主榜学习。
        return True
    annotated = _annotate_first_board_timing(item)
    factors = annotated.get("probability_factors") or {}
    if bool(annotated.get("prediction_shape_seed") or factors.get("prediction_shape_seed")):
        # 启动形态种子即使尚未满足交易闸门也要落入预测样本，否则每日学习
        # 永远看不到“预测到了形态、但没有到买点”这类关键反例。
        return True
    route = str(annotated.get("candidate_route") or "").strip()
    if route in FIRST_BOARD_LEGACY_OBSERVATION_RECORD_ROUTES:
        return False
    if bool(annotated.get("keep_in_diagnostics")) or annotated.get("trade_ready") is False:
        return False
    return _is_displayable_first_board_prediction(annotated)


def _prediction_record_snapshot_source(
    record: PromotionPredictionRecord,
    factors: dict | None = None,
) -> str:
    factors = factors or _json_loads_safe(record.factors_json)
    explicit = str(getattr(record, "snapshot_source", "") or "").strip()
    return _normalize_prediction_snapshot_source(
        explicit if explicit and explicit != "legacy" else factors.get("prediction_snapshot_source")
    )


def _prediction_record_snapshot_context(
    record: PromotionPredictionRecord,
    factors: dict | None = None,
) -> str:
    factors = factors or _json_loads_safe(record.factors_json)
    explicit = str(getattr(record, "snapshot_context", "") or "").strip().lower()
    if explicit and explicit != "legacy":
        return _normalize_prediction_snapshot_context(explicit)
    return _normalize_prediction_snapshot_context(
        factors.get("prediction_snapshot_context"),
        source=_prediction_record_snapshot_source(record, factors),
    )


def _prediction_record_model_version(
    record: PromotionPredictionRecord,
    factors: dict | None = None,
) -> str:
    factors = factors or _json_loads_safe(record.factors_json)
    return str(
        getattr(record, "model_version", "")
        or factors.get("prediction_model_version")
        or "legacy"
    ).strip()


def _prediction_record_snapshot_time(record: PromotionPredictionRecord) -> datetime:
    explicit = getattr(record, "snapshot_recorded_at", None)
    if isinstance(explicit, datetime):
        return explicit
    factors = _json_loads_safe(record.factors_json)
    parsed = _parse_prediction_snapshot_recorded_at(factors.get("prediction_snapshot_recorded_at"))
    if parsed is not None:
        return parsed
    return record.updated_at or record.created_at or datetime.min


def _prediction_record_snapshot_batch_key(record: PromotionPredictionRecord) -> str:
    explicit = str(getattr(record, "snapshot_batch_key", "") or "").strip()
    if explicit:
        return explicit
    return _prediction_snapshot_batch_key_from_factors(_json_loads_safe(record.factors_json))


def _latest_learning_batch_records(records: list[PromotionPredictionRecord]) -> list[PromotionPredictionRecord]:
    """Select one prior-close batch per date/lane; preserve intraday batches for replay only.

    The official daily score is the previous-close forecast. 20:00 is preferred,
    15:10 is its fallback, and 09:25/09:35 confirmations must never replace it.
    """
    grouped: dict[tuple[date, int], list[PromotionPredictionRecord]] = {}
    passthrough: list[PromotionPredictionRecord] = []
    for record in records:
        factors = _json_loads_safe(record.factors_json)
        source = _prediction_record_snapshot_source(record, factors)
        if source != "schedule" or not record.prediction_trade_date:
            passthrough.append(record)
            continue
        grouped.setdefault((record.prediction_trade_date, _safe_int(record.target_board)), []).append(record)

    selected: list[PromotionPredictionRecord] = list(passthrough)
    for batch_records in grouped.values():
        records_by_context: dict[str, list[PromotionPredictionRecord]] = defaultdict(list)
        for record in batch_records:
            records_by_context[_prediction_record_snapshot_context(record)].append(record)

        chosen_context = next(
            (context for context in PROMOTION_CANONICAL_CLOSE_CONTEXTS if records_by_context.get(context)),
            "",
        )
        if not chosen_context:
            # 缺少正式收盘批次时保留缺口，不能用盘中确认或未知上下文
            # 冒充前收盘预测；盘中记录仍由独立回放链路读取。
            continue

        context_records = records_by_context[chosen_context]
        latest_time = max(_prediction_record_snapshot_time(record) for record in context_records)
        latest_batch_records = [
            record
            for record in context_records
            if _prediction_record_snapshot_time(record) == latest_time
        ]
        # Hand-authored/legacy fixtures may not share timestamps. If a batch key is
        # available, use it as the authoritative grouping identity.
        latest_record = max(context_records, key=_prediction_record_snapshot_time)
        latest_batch_key = _prediction_record_snapshot_batch_key(latest_record)
        if latest_batch_key and not latest_batch_key.endswith(":"):
            keyed = [
                record
                for record in context_records
                if _prediction_record_snapshot_batch_key(record) == latest_batch_key
            ]
            if keyed:
                latest_batch_records = keyed
        selected.extend(latest_batch_records)
    return selected


def _annotate_prediction_record_metadata(
    candidates: list[dict],
    ranked_candidates: list[dict],
    *,
    ranked_limit: int,
    recall_ranked_candidates: list[dict] | None = None,
    recall_ranked_limit: int = 0,
    rank_eligible_candidates: list[dict] | None = None,
    snapshot_source: str = "page",
    snapshot_context: str = "",
    news_end_time: datetime | None = None,
    recorded_at: datetime | None = None,
    candidate_anchor_trade_date: date | None = None,
) -> list[dict]:
    normalized_source = _normalize_prediction_snapshot_source(snapshot_source)
    normalized_context = _normalize_prediction_snapshot_context(
        snapshot_context,
        source=normalized_source,
    )
    recorded_at_text = (recorded_at or datetime.now()).isoformat(timespec="seconds")
    batch_key = f"{normalized_source}:{normalized_context}:{recorded_at_text}"
    is_close_snapshot = bool(
        normalized_source == "schedule"
        and normalized_context in PROMOTION_CANONICAL_CLOSE_CONTEXTS
    )
    is_intraday_snapshot = bool(
        normalized_source == "schedule"
        and normalized_context in PROMOTION_INTRADAY_CONTEXTS
    )
    ranked_positions: dict[tuple[str, int, str], int] = {}
    ranked_items_by_key: dict[tuple[str, int, str], dict] = {}
    for index, ranked_item in enumerate(ranked_candidates[:ranked_limit], start=1):
        ranked_key = _prediction_record_key(ranked_item)
        if not ranked_key[0]:
            continue
        if (
            _safe_int(ranked_item.get("target_board"), 1) == 1
            and not bool(ranked_item.get("prediction_rank_eligible"))
            and not _is_actionable_first_board_prediction(ranked_item)
        ):
            continue
        ranked_positions[ranked_key] = index
        ranked_items_by_key[ranked_key] = ranked_item
    normalized_recall_limit = max(_safe_int(recall_ranked_limit), 0)
    recall_positions: dict[tuple[str, int, str], int] = {}
    for index, recall_item in enumerate(
        (recall_ranked_candidates or [])[:normalized_recall_limit],
        start=1,
    ):
        recall_key = _prediction_record_key(recall_item)
        if recall_key[0]:
            recall_positions[recall_key] = index
    rank_eligible_keys = {
        key
        for candidate in (rank_eligible_candidates or [])
        if (key := _prediction_record_key(candidate))[0]
    }
    if rank_eligible_candidates is None:
        rank_eligible_keys = set(ranked_positions) | set(recall_positions)
    pool_size = len(candidates)
    enriched: list[dict] = []
    for pool_index, item in enumerate(candidates, start=1):
        key = _prediction_record_key(item)
        if not key[0]:
            continue
        ranked_position = ranked_positions.get(key, 0)
        ranked_selected = ranked_position > 0
        recall_ranked_position = recall_positions.get(key, 0)
        recall_ranked_selected = recall_ranked_position > 0
        ranked_item = ranked_items_by_key.get(key) or {}
        execution_item = ranked_item or item
        target_board = _safe_int(item.get("target_board"), 1)
        trade_gate_passed = bool(execution_item.get("trade_ready"))
        formal_actionable = bool(
            ranked_selected
            and (
                execution_item.get("prediction_actionable")
                if target_board == 1
                else execution_item.get("trade_ready")
            )
        )
        execution_metadata = {
            "prediction_trade_gate_passed": trade_gate_passed,
            "prediction_actionable": formal_actionable,
            # === 2026-09-18 可诊断性：把"没入榜"与"路线说不"分开 ===
            # `formal_actionable = ranked_selected and (...)`，于是 `actionable=0`
            # 同时可能是两种完全不同的原因：候选没进正式榜，或路线级判定就是否。
            # 实测 9/16 有 3 只 mainline 候选 `rank_scope=ranked` 但 `watch_only=1`
            # ——即已入榜却仍被判不可执行；而 9/17 全部 8 只 `pool_unranked`。
            # 两者的处置方式完全不同（前者要查门槛、后者要查排序），此前混在一个
            # 0 里，是排查缓慢的直接原因。这两个字段是**纯附加**，不参与任何判定。
            "prediction_ranked_selected": ranked_selected,
            "prediction_formal_actionable": formal_actionable,
            "prediction_route_actionable": (
                not bool(execution_item.get("prediction_watch_only"))
            ),
            # 首板路线的两级判定分开留证（纯附加，不参与判定）：
            #   route_level_actionable = `_is_actionable_first_board_prediction`
            #   active_confirmation     = 硬新闻或已验证竞价证据
            #   not_actionable_reasons  = 实际判负的子条件（按发生顺序）
            "prediction_route_level_actionable": bool(
                execution_item.get("prediction_route_level_actionable")
                if execution_item.get("prediction_route_level_actionable") is not None
                else execution_item.get("prediction_actionable")
            ),
            "prediction_active_confirmation": execution_item.get(
                "prediction_active_confirmation"
            ),
            "prediction_not_actionable_reasons": list(
                execution_item.get("prediction_not_actionable_reasons") or []
            )[:8],
            "prediction_watch_only": bool(
                execution_item.get("prediction_watch_only")
                or execution_item.get("prediction_only")
            ),
            "prediction_only": bool(execution_item.get("prediction_only")),
            "trade_actionability_score": _safe_float(
                execution_item.get("trade_actionability_score")
            ),
            "trade_actionability_status": str(
                execution_item.get("trade_actionability_status") or ""
            ),
            "trade_actionability_label": str(
                execution_item.get("trade_actionability_label")
                or execution_item.get("prediction_actionability_label")
                or ""
            ),
            "prediction_rank_lane": str(execution_item.get("rank_lane") or ""),
            "prediction_rank_lane_label": str(
                execution_item.get("rank_lane_label") or ""
            ),
            "prediction_rank_active_confirmation": bool(
                execution_item.get("rank_active_confirmation")
            ),
        }
        item_factors = dict(item.get("probability_factors") or {})
        if candidate_anchor_trade_date is not None:
            item_factors["prediction_candidate_anchor_trade_date"] = (
                candidate_anchor_trade_date.isoformat()
            )
        factors = {
            **item_factors,
            "prediction_model_version": str(
                item_factors.get("prediction_model_version") or PROMOTION_MODEL_VERSION
            ),
            "prediction_calibration_version": str(
                item_factors.get("prediction_calibration_version")
                or PROMOTION_CALIBRATION_VERSION
            ),
            **execution_metadata,
            "prediction_empirical_lift_rank_score": (
                _first_board_empirical_lift_rank_score(item)
                if _safe_int(item.get("target_board"), 1) == 1
                else None
            ),
            "prediction_historical_ignition_rank_score": (
                _first_board_empirical_lift_rank_score(item)
                if _safe_int(item.get("target_board"), 1) == 1
                else None
            ),
            "prediction_pool_rank": pool_index,
            "prediction_pool_size": pool_size,
            "prediction_rank_contract_version": "promotion_rank_contract_v1",
            "prediction_rank_contract_complete": True,
            "prediction_rank_eligible": key in rank_eligible_keys,
            "prediction_rank_eligible_count": len(rank_eligible_keys),
            "prediction_ranked_selected": ranked_selected,
            "prediction_ranked_position": ranked_position,
            "prediction_ranked_limit": ranked_limit,
            "prediction_recall_ranked_selected": recall_ranked_selected,
            "prediction_recall_ranked_position": recall_ranked_position,
            "prediction_recall_ranked_limit": normalized_recall_limit,
            "prediction_record_scope": (
                "ranked"
                if ranked_selected
                else "recall_ranked"
                if recall_ranked_selected
                else "pool_unranked"
            ),
            "prediction_snapshot_source": normalized_source,
            "prediction_snapshot_context": normalized_context,
            "prediction_snapshot_recorded_at": recorded_at_text,
            "prediction_snapshot_batch_key": batch_key,
            "prediction_news_end_time": (
                news_end_time.isoformat(timespec="seconds")
                if isinstance(news_end_time, datetime)
                else None
            ),
            "prediction_snapshot_phase": (
                "previous_close_forecast"
                if is_close_snapshot
                else "intraday_confirmation"
                if is_intraday_snapshot
                else "ad_hoc"
            ),
            "prediction_canonical_snapshot": is_close_snapshot,
            "learning_eligible": ranked_selected and is_close_snapshot,
            "confirmation_eligible": ranked_selected and is_intraday_snapshot,
        }
        enriched.append(
            {
                **item,
                **execution_metadata,
                "probability_factors": factors,
            }
        )
    enriched, _direction_metadata = annotate_direction_research(enriched)
    # The persistence adapter applies recordability again. Every annotated
    # candidate (including noneligible names) must survive: candidate_count is
    # part of the frozen direction contract, not only the selected universe.
    persisted_keys = {
        _prediction_record_key(item) for item in enriched
        if _is_recordable_prediction_candidate(item)
    }
    missing_eligible = rank_eligible_keys - persisted_keys
    missing_recordable = ({
        _prediction_record_key(item) for item in enriched
        if _safe_int(item.get("target_board"), 1) == 1
    } - persisted_keys) | missing_eligible
    if missing_recordable:
        for item in enriched:
            proof = (item.get("probability_factors") or {}).get("direction_research")
            if proof is not None:
                proof.update({
                    "rank_contract_complete": False,
                    "selected": False,
                    "rank_position": None,
                    "selected_count": 0,
                    "error": ("eligible_candidates_not_recordable" if missing_eligible
                              else "direction_candidates_not_recordable"),
                    "missing_recordable_count": len(missing_recordable),
                })
    return enriched


def _direction_research_payload(candidates: list[dict]) -> dict:
    """Project only new annotated records; never backfill cached/old snapshots."""
    lane = [item for item in candidates if _safe_int(item.get("target_board"), 1) == 1]
    proofs = [(item.get("probability_factors") or {}).get("direction_research") for item in lane]
    if not lane or any(not isinstance(proof, dict) for proof in proofs):
        return {"status": "unavailable", "reason": "direction_research_contract_missing",
                "candidates": [], "research_only": True, "frozen": False,
                "target_precision": 0.8}
    # Pure recomputation supplies display metadata only; persisted proof wins
    # when recordability validation blocked the rank.
    _, metadata = annotate_direction_research(lane)
    errors = sorted({proof.get("error") or "direction_research_contract_incomplete"
                     for proof in proofs if proof.get("rank_contract_complete") is not True})
    metadata.update({"research_only": True, "frozen": False,
                     "persistence_status": "not_verified"})
    if errors:
        metadata.update({"status": "blocked", "reason": errors[0],
                         "candidates": [], "selected_count": 0})
    return metadata


def _first_board_temporal_probability(item: dict) -> float:
    """用严格 T-1 可见特征估计次日首板事件概率。

    系数只在 2026-06-09..2026-08-10 训练，并在 2026-08-12..2026-08-27
    时间外留出集验证。所有输入先按训练期物理范围裁剪，避免单个异常字段把
    低基准率事件推成虚高概率；该概率不代表交易可执行性。
    """
    factors = item.get("probability_factors") or {}
    detail = item.get("detail") or {}

    def feature(name: str, default: float = 0.0) -> float:
        if name in factors:
            return _safe_float(factors.get(name), default)
        if name in item:
            return _safe_float(item.get(name), default)
        return _safe_float(detail.get(name), default)

    memory_score = min(max(feature("memory_score"), 0.0), 100.0)
    sector_limit_up_delta = min(max(feature("sector_limit_up_delta"), -5.0), 12.0)
    upper_gap_pct = min(max(feature("upper_gap_pct"), 0.0), 8.0)
    lower_gap_pct = min(max(feature("lower_gap_pct"), 0.0), 8.0)
    platform_score = min(max(feature("platform_score"), 0.0), 100.0)
    support_squeeze_score = min(
        max(feature("support_squeeze_signal_score"), 0.0),
        100.0,
    )
    kline_confirmation_score = min(
        max(feature("kline_confirmation_score"), 0.0),
        1.0,
    )
    limit_probe = 1.0 if bool(
        factors.get("limit_probe")
        or item.get("limit_probe")
        or detail.get("limit_probe")
    ) else 0.0
    qualified_doji = 1.0 if bool(
        factors.get("qualified_doji_confirmation")
        or item.get("qualified_doji_confirmation")
        or detail.get("qualified_doji_confirmation")
    ) else 0.0
    volume_suffocation = 1.0 if bool(
        factors.get("has_volume_suffocation")
        or item.get("has_volume_suffocation")
        or detail.get("has_volume_suffocation")
    ) else 0.0

    log_odds = (
        -3.2532858285
        + memory_score * 0.0152762376
        + limit_probe * 0.2581869734
        + sector_limit_up_delta * 0.0201322352
        + upper_gap_pct * 0.0366834883
        + lower_gap_pct * 0.0194743530
        - platform_score * 0.0066157975
        - support_squeeze_score * 0.0270909334
        + kline_confirmation_score * 0.4104064963
        - qualified_doji * 0.1445161493
        - volume_suffocation * 0.1032808018
    )
    probability = 1.0 / (1.0 + exp(-log_odds))
    return round(min(max(probability, 0.001), 0.95), 4)


def _apply_promotion_learning_to_candidates(candidates: list[dict], stats: dict[str, dict]) -> list[dict]:
    adjusted: list[dict] = []
    for item in candidates:
        target_board = _safe_int(item.get("target_board"), 1)
        route = str(item.get("candidate_route") or "")
        bucket = (
            str(item.get("learning_bucket") or "").strip()
            or _promotion_learning_bucket(target_board, route)
        ).split("|state:", 1)[0]
        route_bucket = _promotion_learning_bucket(target_board, route)
        item_factors = item.get("probability_factors") or {}
        context_bucket = _promotion_context_learning_bucket(bucket, item_factors)
        contextual_learning = stats.get(context_bucket) or {}
        if _safe_int(contextual_learning.get("sample_count")) >= 12:
            learning = contextual_learning
            effective_learning_bucket = context_bucket
        else:
            learning = stats.get(bucket) or stats.get(route_bucket) or {}
            effective_learning_bucket = bucket if bucket in stats else route_bucket
        # 在任何数值默认/裁剪之前验原始概率；否则缺失会先被洗成0，
        # 下游持久化再严格校验也无法识别原本没有生产输入。
        original_probability = validate_probability(item.get("probability"), field="probability")
        sample_count = _safe_int(learning.get("sample_count"))
        if "empirical_probability" in learning:
            empirical_probability = validate_probability(learning["empirical_probability"], field="empirical_probability")
        elif sample_count >= 4:
            success_count = _safe_int(learning.get("success_count"))
            prior_alpha, prior_beta = ((1.0, 19.0) if target_board == 1 else (2.0, 8.0))
            empirical_probability = (
                success_count + prior_alpha
            ) / (
                sample_count + prior_alpha + prior_beta
            )
        else:
            empirical_probability = original_probability
        max_residual_weight = (
            PROMOTION_FIRST_BOARD_MAX_RESIDUAL_WEIGHT
            if target_board == 1
            else PROMOTION_SECOND_BOARD_MAX_RESIDUAL_WEIGHT
        )
        residual_weight = min(
            max_residual_weight,
            max_residual_weight * sample_count / 40.0,
        ) if sample_count >= 4 else 0.0
        # 以路线真实后验命中率为中心，只保留一部分个股相对 log-odds 残差。
        # 原始分是排序分而非严格概率，不能让极端原始分把1%~16%的基准率重新
        # 放大到40%~90%；同时又不能直接用路线均值覆盖所有个股。
        epsilon = 1e-4
        bounded_original = min(max(original_probability, epsilon), 1.0 - epsilon)
        bounded_empirical = min(max(empirical_probability, epsilon), 1.0 - epsilon)
        avg_predicted = min(
            max(
                validate_probability(learning["avg_predicted_probability"], field="avg_predicted_probability")
                if "avg_predicted_probability" in learning else bounded_original,
                epsilon,
            ),
            1.0 - epsilon,
        )
        empirical_log_odds = log(bounded_empirical / (1.0 - bounded_empirical))
        original_log_odds = log(bounded_original / (1.0 - bounded_original))
        average_log_odds = log(avg_predicted / (1.0 - avg_predicted))
        route_log_odds_shift = empirical_log_odds - average_log_odds
        calibrated_log_odds = empirical_log_odds + (
            original_log_odds - average_log_odds
        ) * residual_weight
        route_calibrated_probability = _clamp_probability(
            1.0 / (1.0 + exp(-calibrated_log_odds))
        )
        temporal_probability = (
            _first_board_temporal_probability(item)
            if target_board == 1
            else route_calibrated_probability
        )
        calibrated_probability = (
            _clamp_probability(
                temporal_probability * 0.75
                + route_calibrated_probability * 0.25
            )
            if target_board == 1
            else route_calibrated_probability
        )
        adjustment = calibrated_probability - original_probability
        confidence_level, confidence_score, confidence_label = _confidence_from_probability(calibrated_probability)
        selection_recall = _safe_float(learning.get("selection_recall"))
        pool_success_count = _safe_int(learning.get("pool_success_count"))
        recall_rank_boost = 0.0
        if target_board == 1 and pool_success_count >= PROMOTION_LEARNING_MIN_RECALL_SUCCESS:
            recall_rank_boost = min(
                PROMOTION_LEARNING_MAX_RECALL_RANK_BOOST,
                max(0.30 - selection_recall, 0.0) * 25.0,
            )
        calibrated_sub_probabilities = {
            **(item.get("sub_probabilities") or {}),
            ("next_second_board" if target_board == 2 else "first_limitup_next_day"): calibrated_probability,
        }
        if target_board == 1:
            # 优先使用全池口径的上涨/强涨先验。当主榜样本较少时，仅用 ranked
            # 子集会让 next_day_rise 反复抖动；补上 pool_empirical_* 后，
            # 既能与真实命中率对齐，也能在主榜 recall < 30% 的早期阶段保持
            # 显示稳定。
            if _safe_int(learning.get("pool_sample_count")) >= 12:
                rising_probability = _safe_float(
                    learning.get("pool_empirical_rising_probability"),
                    _safe_float(learning.get("empirical_rising_probability"), 0.5),
                )
                strong_rising_probability = _safe_float(
                    learning.get("pool_empirical_strong_rising_probability"),
                    _safe_float(learning.get("empirical_strong_rising_probability"), 0.1),
                )
            else:
                rising_probability = _safe_float(
                    learning.get("empirical_rising_probability"),
                    0.5,
                )
                strong_rising_probability = _safe_float(
                    learning.get("empirical_strong_rising_probability"),
                    0.1,
                )
            # 路由后验是主口径，只保留极小的个股原始分相对残差用于同路由
            # 排序，避免再把形态分直接映射成70%~90%的虚高上涨概率。
            directional_residual = (bounded_original - avg_predicted) * min(
                0.12,
                residual_weight * 0.30,
            )
            precursor_direction_delta = max(
                -0.12,
                min(_safe_float(item_factors.get("launch_direction_logit_delta")), 0.18),
            )
            precursor_strong_rise_delta = max(
                -0.18,
                min(
                    _safe_float(
                        item_factors.get("launch_strong_rise_logit_delta"),
                        precursor_direction_delta * 0.70,
                    ),
                    0.32,
                ),
            )
            rising_probability = apply_logit_delta(
                rising_probability + directional_residual,
                precursor_direction_delta,
            )
            strong_rising_probability = apply_logit_delta(
                strong_rising_probability + directional_residual * 0.55,
                precursor_strong_rise_delta,
            )
            rising_probability = min(max(rising_probability, 0.12), 0.88)
            strong_rising_probability = min(max(strong_rising_probability, 0.03), 0.65)
            calibrated_sub_probabilities["next_day_rise"] = round(
                rising_probability,
                4,
            )
            calibrated_sub_probabilities["next_day_strong_rise"] = round(
                strong_rising_probability,
                4,
            )
        if target_board == 1 and sample_count >= 4:
            # 3/5/10日原始分过去没有独立结果标签，不能继续把90%形态分冒充真实概率。
            # 先由已验证的次日概率推导保守上限；后续积累独立 horizon 标签后再分别校准。
            effective_five_day_probability = _clamp_probability(
                1.0 - (1.0 - calibrated_probability) ** 3.5
            )
            calibrated_sub_probabilities["first_limitup_5d"] = min(
                _safe_float(calibrated_sub_probabilities.get("first_limitup_5d"), 1.0),
                effective_five_day_probability,
            )
            calibrated_sub_probabilities["breakout_3d"] = min(
                _safe_float(calibrated_sub_probabilities.get("breakout_3d"), 1.0),
                _clamp_probability(effective_five_day_probability + 0.18),
            )
            calibrated_sub_probabilities["become_core_10d"] = min(
                _safe_float(calibrated_sub_probabilities.get("become_core_10d"), 1.0),
                _clamp_probability(effective_five_day_probability * 0.45),
            )
        probability_factors = {
            **item_factors,
            "learning_bucket": effective_learning_bucket,
            "learning_context_bucket": context_bucket,
            "learning_context_sample_sufficient": _safe_int(contextual_learning.get("sample_count")) >= 12,
            "learning_sample_count": sample_count,
            "learning_success_rate": _safe_float(learning.get("success_rate")),
            "learning_rising_rate": learning.get("rising_rate"),
            "learning_directional_sample_count": _safe_int(learning.get("directional_sample_count")),
            "learning_directional_unknown_count": _safe_int(learning.get("directional_unknown_count")),
            "learning_directional_coverage": _safe_float(learning.get("directional_coverage")),
            "learning_directional_evidence_status": learning.get("directional_evidence_status", "unavailable"),
            "learning_strong_rising_rate": _safe_float(learning.get("strong_rising_rate")),
            "learning_avg_max_change_pct": _safe_float(learning.get("avg_max_change_pct")),
            "learning_avg_close_change_pct": learning.get("avg_close_change_pct"),
            "learning_empirical_probability": empirical_probability,
            "learning_empirical_rising_probability": _safe_float(
                learning.get("empirical_rising_probability"),
                0.5,
            ),
            "learning_empirical_strong_rising_probability": _safe_float(
                learning.get("empirical_strong_rising_probability"),
                0.1,
            ),
            "learning_calibration_weight": round(residual_weight, 3),
            "learning_calibration_method": (
                PROMOTION_FIRST_BOARD_TEMPORAL_CALIBRATION_VERSION
                if target_board == 1
                else PROMOTION_CALIBRATION_VERSION
            ),
            "route_calibrated_probability": route_calibrated_probability,
            "temporal_event_probability": temporal_probability,
            "temporal_route_blend_weights": (
                {"temporal": 0.75, "route": 0.25}
                if target_board == 1
                else {"temporal": 0.0, "route": 1.0}
            ),
            "first_board_temporal_calibration_version": (
                PROMOTION_FIRST_BOARD_TEMPORAL_CALIBRATION_VERSION
                if target_board == 1
                else None
            ),
            "learning_route_log_odds_shift": round(route_log_odds_shift, 4),
            "learning_brier_score": _safe_float(learning.get("brier_score")),
            "learning_selection_recall": _safe_float(learning.get("selection_recall")),
            "learning_missed_success_count": _safe_int(learning.get("missed_success_count")),
            "learning_recall_rank_boost": round(recall_rank_boost, 3),
            "learning_probability_adjustment": adjustment,
            "learning_raw_probability_adjustment": _safe_float(learning.get("model_adjustment")),
            "learning_adjustment_capped": False,
            "learning_adjustment_floor": None,
            "prediction_model_version": PROMOTION_MODEL_VERSION,
            "promotion_event_label_version": PROMOTION_LABEL_VERSION,
            "learning_direction_precursor_delta_applied": (
                round(precursor_direction_delta, 4) if target_board == 1 else 0.0
            ),
            "learning_strong_rise_precursor_delta_applied": (
                round(precursor_strong_rise_delta, 4) if target_board == 1 else 0.0
            ),
            "learning_multi_horizon_method": (
                "derived_conservative_ceiling_from_calibrated_next_day"
                if target_board == 1 and sample_count >= 4
                else "raw_insufficient_sample"
            ),
            "learning_direction_probability_method": (
                "beta_binomial_route_direction_with_outcome_specific_precursor_delta"
                if target_board == 1 and sample_count >= 4
                else "neutral_prior_insufficient_sample"
            ),
        }
        adjusted.append(
            {
                **item,
                "raw_probability": original_probability,
                "route_calibrated_probability": route_calibrated_probability,
                "temporal_event_probability": temporal_probability,
                "probability_calibration_version": (
                    PROMOTION_FIRST_BOARD_TEMPORAL_CALIBRATION_VERSION
                    if target_board == 1
                    else PROMOTION_CALIBRATION_VERSION
                ),
                "probability": calibrated_probability,
                "limit_up_probability": calibrated_probability,
                "direction_probability": (
                    _safe_float(calibrated_sub_probabilities.get("next_day_rise"))
                    if target_board == 1
                    else None
                ),
                "strong_rise_probability": (
                    _safe_float(calibrated_sub_probabilities.get("next_day_strong_rise"))
                    if target_board == 1
                    else None
                ),
                "forecast_probability_scope": (
                    "next_day_first_limit_up"
                    if target_board == 1
                    else "next_day_second_board"
                ),
                "sub_probabilities": calibrated_sub_probabilities,
                "confidence": confidence_score,
                "confidence_level": confidence_level,
                "confidence_label": confidence_label,
                "learning_bucket": effective_learning_bucket,
                "learning_stats": learning,
                "model_adjustment": adjustment,
                "learning_recall_rank_boost": round(recall_rank_boost, 3),
                "probability_factors": probability_factors,
            }
        )
    return adjusted


async def _record_promotion_predictions(
    db: AsyncSession,
    candidates: list[dict],
    trade_date_by_target: dict[int, date],
    *,
    snapshot_source: str = "page",
    quality_gate: dict | None = None,
    return_details: bool = False,
    schedule_batch: ScheduleBatch | None = None,
):
    """Persist via the compatibility adapter and return the exact ledger run on demand."""

    result = await _persist_promotion_predictions(
        db,
        candidates,
        trade_date_by_target,
        snapshot_source=snapshot_source,
        model_version=PROMOTION_MODEL_VERSION,
        model_identity=PROMOTION_MODEL_IDENTITY,
        is_recordable=_is_recordable_prediction_candidate,
        reason_builder=_build_prediction_reason_snapshot,
        learning_bucket_builder=_promotion_learning_bucket,
        quality_gate=quality_gate,
        schedule_batch=schedule_batch,
    )
    return result if return_details else result.touched


async def _load_stock_tag_map(db: AsyncSession, codes: list[str]) -> dict[str, StockTag]:
    normalized_codes = list(dict.fromkeys(str(code or "").strip() for code in codes if str(code or "").strip()))
    if not normalized_codes:
        return {}
    result = await db.execute(select(StockTag).where(StockTag.code.in_(normalized_codes)))
    return {str(item.code or "").strip(): item for item in result.scalars().all()}


def _select_best_prediction_record(records: list[PromotionPredictionRecord]) -> PromotionPredictionRecord | None:
    if not records:
        return None
    records = [record for record in records if not _is_legacy_observation_first_board_record(record)]
    if not records:
        return None
    def rank_key(record: PromotionPredictionRecord) -> tuple:
        factors = _json_loads_safe(record.factors_json)
        snapshot_source = _prediction_record_snapshot_source(record, factors)
        snapshot_context = _prediction_record_snapshot_context(record, factors)
        snapshot_time = _prediction_record_snapshot_time(record)
        context_priority = (
            4
            if snapshot_context == "promotion_2000"
            else 3
            if snapshot_context == "promotion_1510"
            else 2
            if snapshot_context in PROMOTION_INTRADAY_CONTEXTS
            else 1
        )
        return (
            1 if snapshot_source == "schedule" else 0,
            context_priority,
            snapshot_time,
            1 if factors.get("prediction_ranked_selected", True) is not False else 0,
            _safe_int(factors.get("prediction_ranked_limit")),
            _safe_float(record.calibrated_probability, _safe_float(record.predicted_probability)),
            _safe_float(factors.get("route_score")),
        )
    return max(
        records,
        key=rank_key,
    )


def _is_intraday_first_board_confirmation_record(record: PromotionPredictionRecord, actual_trade_date: date) -> bool:
    if _safe_int(record.target_board) != 1:
        return False
    if record.prediction_trade_date != actual_trade_date:
        return False
    factors = _json_loads_safe(record.factors_json)
    context = _prediction_record_snapshot_context(record, factors)
    source = _prediction_record_snapshot_source(record, factors)
    return source == "schedule" and context in PROMOTION_INTRADAY_CONTEXTS


def _resolve_actual_replay_status(
    *,
    code: str,
    target_board: int,
    record: PromotionPredictionRecord | None,
    tag: StockTag | None,
    previous_first_board_found: bool = False,
    snapshot_incomplete: bool = False,
    snapshot_record_count: int = 0,
    snapshot_min_record_count: int = 0,
) -> tuple[str, str, str]:
    if record is None:
        board_tag = str(getattr(tag, "board_tag", "") or "")
        if board_tag in {"blocked", "suspended"}:
            return (
                "filtered",
                "被风控/股票标签过滤",
                f"预测池已按 StockTag 过滤该股，标签为 {board_tag}。",
            )
        if not stock_tagger.is_tradeable(code):
            return (
                "filtered",
                "非主板观察标的",
                "该股不属于当前主板可交易优先范围，预测只做观察或不进入交易榜。",
            )
        if snapshot_incomplete:
            return (
                "snapshot_incomplete",
                "预测快照不完整",
                (
                    f"上一交易日该赛道只留下 {snapshot_record_count} 条预测记录，"
                    f"低于最低复盘口径 {snapshot_min_record_count} 条，不能把缺失样本简单归因为未入池。"
                ),
            )
        if target_board == 2 and previous_first_board_found:
            return (
                "not_in_pool",
                "前日首板存在但未写入二板预测记录",
                "上一交易日过滤后首板池已有该股，但调度快照没有留下二板预测记录，优先排查候选记录/调度快照缺口。",
            )
        return (
            "not_in_pool",
            "未进入预测池",
            "前一交易日没有留下该方向预测记录，通常表示消息/竞价/主线扩散触发没有被当时数据捕捉到。",
        )

    factors = _json_loads_safe(record.factors_json)
    probability = _safe_float(record.calibrated_probability, _safe_float(record.predicted_probability))
    route_score = _safe_float(factors.get("route_score"))
    ranked_selected = factors.get("prediction_ranked_selected", True) is not False
    pool_rank = _safe_int(factors.get("prediction_pool_rank"))
    ranked_limit = _safe_int(factors.get("prediction_ranked_limit"))

    if not ranked_selected:
        if target_board == 1 and (probability < 0.10 or route_score < 42.0):
            return (
                "score_low",
                "入池但分数偏低",
                f"前一交易日已入预测池，但概率 {probability * 100:.1f}% / 路线分 {route_score:.1f}，没有达到有效买点层。",
            )
        if target_board == 2 and probability < 0.08:
            return (
                "score_low",
                "入池但晋级分偏低",
                f"前一交易日已入二板池，但概率仅 {probability * 100:.1f}%，被视为低确定性晋级。",
            )
        return (
            "rank_cutoff",
            "入池但被排序挤掉",
            f"前一交易日已入池，但池内排名 {pool_rank or '--'}，未进入 ranked_limit={ranked_limit or '--'} 的主榜。",
        )

    if target_board == 1 and probability < 0.10:
        return (
            "predicted_hit",
            "预测命中（低概率）",
            f"已进入正式主榜并真实涨停，统一计为命中；概率仅 {probability * 100:.1f}%，另记为低估校准样本。",
        )
    if target_board == 2 and probability < 0.08:
        return (
            "predicted_hit",
            "二板预测命中（低概率）",
            f"已进入二板正式主榜并真实晋级，统一计为命中；概率仅 {probability * 100:.1f}%，另记为低估校准样本。",
        )
    return (
        "predicted_hit",
        "预测命中",
        "前一交易日已进入对应主榜，属于有效覆盖样本。",
    )


async def _build_actual_limit_up_replay(
    db: AsyncSession,
    *,
    actual_trade_date: date,
    actual_limit_ups: list[dict],
) -> dict:
    previous_trade_date = await _get_previous_limit_up_trade_date(db, actual_trade_date)
    first_board_actuals = [item for item in actual_limit_ups if _safe_int(item.get("consecutive_days"), 1) == 1]
    second_board_actuals = [item for item in actual_limit_ups if _safe_int(item.get("consecutive_days"), 1) == 2]
    replay_items_source = [(item, 1) for item in first_board_actuals] + [(item, 2) for item in second_board_actuals]
    if previous_trade_date is None or not replay_items_source:
        return {
            "actual_trade_date": str(actual_trade_date),
            "prediction_trade_date": str(previous_trade_date or ""),
            "summary": {
                "actual_first_board_count": len(first_board_actuals),
                "actual_second_board_count": len(second_board_actuals),
                "predicted_hit_count": 0,
                "previous_close_predicted_hit_count": 0,
                "intraday_confirmation_hit_count": 0,
                "not_in_pool_count": 0,
                "score_low_count": 0,
                "rank_cutoff_count": 0,
                "filtered_count": 0,
                "snapshot_incomplete_count": 0,
                "prediction_record_counts": {"target_1": 0, "target_2": 0},
                "lane_metrics": {
                    "target_1": {"target_label": "首板", "actual_count": len(first_board_actuals), "pool_count": 0, "ranked_count": 0, "ranked_hit_count": 0, "ranked_precision": 0.0, "ranked_recall": 0.0, "hits_at_5": 0, "precision_at_5": 0.0, "hits_at_10": 0, "precision_at_10": 0.0},
                    "target_2": {"target_label": "二板", "actual_count": len(second_board_actuals), "pool_count": 0, "ranked_count": 0, "ranked_hit_count": 0, "ranked_precision": 0.0, "ranked_recall": 0.0, "hits_at_5": 0, "precision_at_5": 0.0, "hits_at_10": 0, "precision_at_10": 0.0},
                },
            },
            "items": [],
            "notes": ["缺少上一交易日或实际涨停样本，暂时无法回放 blocked reason"],
        }

    actual_codes = [str(item.get("code") or "").strip() for item, _target in replay_items_source if str(item.get("code") or "").strip()]
    replay_record_dates = [previous_trade_date]
    if actual_trade_date != previous_trade_date:
        replay_record_dates.append(actual_trade_date)
    record_result = await db.execute(
        select(PromotionPredictionRecord).where(
            PromotionPredictionRecord.prediction_trade_date.in_(replay_record_dates),
            PromotionPredictionRecord.target_board.in_([1, 2]),
            PromotionPredictionRecord.code.in_(actual_codes),
        )
    )
    all_prediction_result = await db.execute(
        select(PromotionPredictionRecord).where(
            PromotionPredictionRecord.prediction_trade_date == previous_trade_date,
            PromotionPredictionRecord.target_board.in_([1, 2]),
        )
    )
    latest_prediction_records = [
        record
        for record in _latest_learning_batch_records(list(all_prediction_result.scalars().all()))
        if not _is_legacy_observation_first_board_record(record)
    ]
    target_record_counts = {
        target_board: sum(
            1 for record in latest_prediction_records
            if _safe_int(record.target_board) == target_board
        )
        for target_board in (1, 2)
    }
    previous_limit_ups = await _load_filtered_limit_ups(db, previous_trade_date)
    previous_first_board_codes = {
        str(item.get("code") or "").strip()
        for item in previous_limit_ups
        if str(item.get("code") or "").strip()
        and _safe_int(item.get("consecutive_days"), 1) == 1
    }
    records_by_key: dict[tuple[str, int], list[PromotionPredictionRecord]] = {}
    for record in record_result.scalars().all():
        if record.prediction_trade_date != previous_trade_date and not _is_intraday_first_board_confirmation_record(record, actual_trade_date):
            continue
        records_by_key.setdefault((str(record.code or "").strip(), _safe_int(record.target_board)), []).append(record)

    tag_map = await _load_stock_tag_map(db, actual_codes)
    items: list[dict] = []
    status_counts: dict[str, int] = {}
    for actual, target_board in replay_items_source:
        code = str(actual.get("code") or "").strip()
        if not code:
            continue
        record = _select_best_prediction_record(records_by_key.get((code, target_board), []))
        snapshot_min_count = (
            PROMOTION_MIN_FIRST_BOARD_SNAPSHOT_RECORDS
            if target_board == 1
            else PROMOTION_MIN_SECOND_BOARD_SNAPSHOT_RECORDS
        )
        snapshot_count = target_record_counts.get(target_board, 0)
        status, blocked_reason, detail = _resolve_actual_replay_status(
            code=code,
            target_board=target_board,
            record=record,
            tag=tag_map.get(code),
            previous_first_board_found=target_board == 2 and code in previous_first_board_codes,
            snapshot_incomplete=snapshot_count < snapshot_min_count,
            snapshot_record_count=snapshot_count,
            snapshot_min_record_count=snapshot_min_count,
        )
        status_counts[status] = status_counts.get(status, 0) + 1
        factors = _json_loads_safe(record.factors_json) if record is not None else {}
        reason_snapshot = _json_loads_safe(record.reason_snapshot) if record is not None else {}
        items.append(
            {
                "code": code,
                "name": str(actual.get("name") or (record.name if record else "") or ""),
                "actual_board": _safe_int(actual.get("consecutive_days"), target_board),
                "target_board": target_board,
                "target_label": "首板" if target_board == 1 else "二板",
                "limit_up_reason": str(actual.get("limit_up_reason") or ""),
                "limit_up_time": str(actual.get("limit_up_time") or ""),
                "seal_amount": _safe_float(actual.get("seal_amount")),
                "break_count": _safe_int(actual.get("break_count")),
                "turnover": _safe_float(actual.get("turnover")),
                "replay_status": status,
                "blocked_reason": blocked_reason,
                "blocked_reason_detail": detail,
                "prediction_found": record is not None,
                "predicted_probability": _safe_float(record.calibrated_probability, _safe_float(record.predicted_probability)) if record else 0.0,
                "low_confidence_hit": bool(
                    status == "predicted_hit"
                    and record is not None
                    and _safe_float(record.calibrated_probability, _safe_float(record.predicted_probability))
                    < (0.10 if target_board == 1 else 0.08)
                ),
                "prediction_snapshot_context": (
                    _prediction_record_snapshot_context(record, factors) if record else ""
                ),
                "prediction_timing": (
                    "intraday_confirmation"
                    if record and _prediction_record_snapshot_context(record, factors) in PROMOTION_INTRADAY_CONTEXTS
                    else "previous_close_forecast"
                    if record
                    else ""
                ),
                "candidate_route": str(record.candidate_route or "") if record else "",
                "candidate_route_label": str(reason_snapshot.get("candidate_route_label") or record.candidate_route or "") if record else "",
                "strategy_lane": str(reason_snapshot.get("strategy_lane") or ""),
                "strategy_lane_label": str(reason_snapshot.get("strategy_lane_label") or ""),
                "prediction_pool_rank": _safe_int(factors.get("prediction_pool_rank")),
                "prediction_pool_size": _safe_int(factors.get("prediction_pool_size")),
                "prediction_ranked_selected": bool(factors.get("prediction_ranked_selected")),
                "prediction_ranked_position": _safe_int(factors.get("prediction_ranked_position")),
                "prediction_ranked_limit": _safe_int(factors.get("prediction_ranked_limit")),
                "prediction_trade_gate_passed": bool(
                    factors.get("prediction_trade_gate_passed")
                ),
                "prediction_actionable": bool(factors.get("prediction_actionable")),
                "trade_actionability_status": str(
                    factors.get("trade_actionability_status") or ""
                ),
                "trade_actionability_label": str(
                    factors.get("trade_actionability_label") or ""
                ),
                "trade_actionability_score": _safe_float(
                    factors.get("trade_actionability_score")
                ),
                "route_score": _safe_float(factors.get("route_score")),
                "second_board_style_score": _safe_float(factors.get("second_board_style_score")),
                "sector_morning_strengthening": bool(factors.get("sector_morning_strengthening")),
                "sector_morning_strengthening_score": _safe_float(factors.get("sector_morning_strengthening_score")),
            }
        )

    actual_codes_by_target = {
        1: {str(item.get("code") or "").strip() for item in first_board_actuals},
        2: {str(item.get("code") or "").strip() for item in second_board_actuals},
    }
    lane_metrics: dict[str, dict] = {}
    for target_board in (1, 2):
        best_records: dict[str, PromotionPredictionRecord] = {}
        for record in latest_prediction_records:
            if _safe_int(record.target_board) != target_board:
                continue
            code = str(record.code or "").strip()
            current = best_records.get(code)
            best_records[code] = _select_best_prediction_record(
                [item for item in (current, record) if item is not None]
            ) or record
        ranked_records = [
            record
            for record in best_records.values()
            if _json_loads_safe(record.factors_json).get("prediction_ranked_selected") is True
        ]
        ranked_records.sort(
            key=lambda record: _safe_int(
                _json_loads_safe(record.factors_json).get("prediction_ranked_position"),
                9999,
            )
        )
        actual_codes_for_lane = actual_codes_by_target[target_board]
        ranked_hit_count = sum(1 for record in ranked_records if str(record.code or "").strip() in actual_codes_for_lane)

        def _precision_at(k: int) -> tuple[int, float]:
            selected = ranked_records[:k]
            hits = sum(1 for record in selected if str(record.code or "").strip() in actual_codes_for_lane)
            return hits, round(hits / len(selected), 4) if selected else 0.0

        hits_at_5, precision_at_5 = _precision_at(5)
        hits_at_10, precision_at_10 = _precision_at(10)
        lane_metrics[f"target_{target_board}"] = {
            "target_label": "首板" if target_board == 1 else "二板",
            "actual_count": len(actual_codes_for_lane),
            "pool_count": len(best_records),
            "ranked_count": len(ranked_records),
            "ranked_hit_count": ranked_hit_count,
            "ranked_precision": round(ranked_hit_count / len(ranked_records), 4) if ranked_records else 0.0,
            "ranked_recall": round(ranked_hit_count / len(actual_codes_for_lane), 4) if actual_codes_for_lane else 0.0,
            "hits_at_5": hits_at_5,
            "precision_at_5": precision_at_5,
            "hits_at_10": hits_at_10,
            "precision_at_10": precision_at_10,
        }

    return {
        "actual_trade_date": str(actual_trade_date),
        "prediction_trade_date": str(previous_trade_date),
        "summary": {
            "actual_first_board_count": len(first_board_actuals),
            "actual_second_board_count": len(second_board_actuals),
            "predicted_hit_count": status_counts.get("predicted_hit", 0),
            "previous_close_predicted_hit_count": sum(
                1
                for item in items
                if item.get("replay_status") == "predicted_hit"
                and item.get("prediction_timing") == "previous_close_forecast"
            ),
            "intraday_confirmation_hit_count": sum(
                1
                for item in items
                if item.get("replay_status") == "predicted_hit"
                and item.get("prediction_timing") == "intraday_confirmation"
            ),
            "not_in_pool_count": status_counts.get("not_in_pool", 0),
            "score_low_count": status_counts.get("score_low", 0),
            "rank_cutoff_count": status_counts.get("rank_cutoff", 0),
            "filtered_count": status_counts.get("filtered", 0),
            "snapshot_incomplete_count": status_counts.get("snapshot_incomplete", 0),
            "prediction_record_counts": {
                "target_1": target_record_counts.get(1, 0),
                "target_2": target_record_counts.get(2, 0),
            },
            "lane_metrics": lane_metrics,
        },
        "items": sorted(
            items,
            key=lambda item: (
                _safe_int(item.get("target_board")),
                str(item.get("replay_status") or ""),
                -_safe_float(item.get("predicted_probability")),
                str(item.get("code") or ""),
            ),
        ),
        "notes": [
            "回放口径：用当日实际首板/二板，对照上一交易日同方向预测记录",
            "状态区分 predicted_hit / not_in_pool / score_low / rank_cutoff / filtered",
            "lane_metrics 分开统计首板/二板的主榜精度、召回率及 Precision@5/10，不再用合并命中数代表预测质量",
            "previous_close_predicted_hit_count 与 intraday_confirmation_hit_count 分开，盘中已发生后的确认不得冒充前一晚预测命中",
            "snapshot_incomplete 表示上一交易日该赛道预测记录过少，需先修快照生成或数据源，不应按未入池复盘",
            "新增后会记录完整候选池元数据；历史上只记录主榜的日期，not_in_pool 可能同时包含未入池和未记录两种情况",
        ],
    }


def _promotion_review_ratio(numerator: int | float, denominator: int | float) -> float:
    return round(_safe_float(numerator) / _safe_float(denominator), 4) if _safe_float(denominator) > 0 else 0.0


def _promotion_directional_metrics(predicted_count: int, evaluable_count: int, hit_count: int) -> dict:
    """Fixed-list bounds; observed-subset precision is not full-list accuracy."""
    unknown = max(predicted_count - evaluable_count, 0)
    complete = predicted_count > 0 and unknown == 0
    observed = _promotion_review_ratio(hit_count, evaluable_count) if evaluable_count else None
    return {
        "directional_evaluable_count": evaluable_count,
        "directional_unknown_count": unknown,
        "directional_coverage": _promotion_review_ratio(evaluable_count, predicted_count),
        "directional_observed_precision": observed,
        "directional_precision_lower_bound": _promotion_review_ratio(hit_count, predicted_count) if predicted_count else None,
        "directional_precision_upper_bound": _promotion_review_ratio(hit_count + unknown, predicted_count) if predicted_count else None,
        "directional_precision": observed if complete else None,
        "directional_target_precision": 0.8,
        "directional_target_met": hit_count / predicted_count >= 0.8 if complete else None,
    }


def _promotion_launch_precursor_cohorts(factors: dict) -> list[str]:
    """Return auditable, non-exclusive launch-evidence cohorts for a first-board record."""
    cohorts = ["all_ranked_first_board"]
    if bool(factors.get("launch_profile_ready")):
        cohorts.append("launch_profile")
    if bool(factors.get("funding_preheat_ready")):
        cohorts.append("funding_preheat")
    if bool(factors.get("primary_industry_ignition_ready")):
        cohorts.append("primary_industry_ignition")
    if bool(factors.get("low_base_sector_ignition_ready")):
        cohorts.append("low_base_industry")
        if bool(factors.get("funding_preheat_ready")):
            cohorts.append("low_base_industry_funding")
    if bool(factors.get("news_direct_high_impact")):
        cohorts.append("direct_high_impact_news")
    if bool(factors.get("news_repeated_direct")):
        cohorts.append("direct_repeated_news")
    if bool(factors.get("funding_persistent_outflow_risk")):
        cohorts.append("persistent_funding_outflow")
    return cohorts


def _promotion_finalize_launch_precursor_metrics(raw_metrics: dict[str, dict]) -> dict[str, dict]:
    finalized: dict[str, dict] = {}
    for cohort, label in PROMOTION_LAUNCH_COHORT_LABELS.items():
        raw = raw_metrics.get(cohort) or {}
        sample_count = _safe_int(raw.get("sample_count"))
        limit_up_hit_count = _safe_int(raw.get("limit_up_hit_count"))
        rise_hit_count = _safe_int(raw.get("rise_hit_count"))
        strong_rise_hit_count = _safe_int(raw.get("strong_rise_hit_count"))
        actionable_count = _safe_int(raw.get("actionable_count"))
        actionable_limit_up_hit_count = _safe_int(raw.get("actionable_limit_up_hit_count"))
        finalized[cohort] = {
            "label": label,
            "sample_count": sample_count,
            "limit_up_hit_count": limit_up_hit_count,
            "limit_up_precision": _promotion_review_ratio(limit_up_hit_count, sample_count),
            "rise_hit_count": rise_hit_count,
            **_promotion_directional_metrics(sample_count, _safe_int(raw.get("directional_evaluable_count")), rise_hit_count),
            "limit_up_evaluable_count": _safe_int(raw.get("limit_up_evaluable_count")),
            "actionable_limit_up_evaluable_count": _safe_int(raw.get("actionable_limit_up_evaluable_count")),
            "strong_rise_hit_count": strong_rise_hit_count,
            "strong_rise_precision": _promotion_review_ratio(strong_rise_hit_count, sample_count),
            "actionable_count": actionable_count,
            "forecast_only_count": max(sample_count - actionable_count, 0),
            "actionable_limit_up_hit_count": actionable_limit_up_hit_count,
            "actionable_limit_up_precision": _promotion_review_ratio(
                actionable_limit_up_hit_count,
                actionable_count,
            ),
        }

    baseline = finalized["all_ranked_first_board"]
    for item in finalized.values():
        if not item["sample_count"] or item["limit_up_evaluable_count"] < item["sample_count"]:
            item["limit_up_precision"] = None
        if not item["actionable_count"] or item["actionable_limit_up_evaluable_count"] < item["actionable_count"]:
            item["actionable_limit_up_precision"] = None
        if item["directional_precision"] is None:
            item["strong_rise_precision"] = None
        item["evaluation_status"] = "complete" if item["sample_count"] and item["directional_unknown_count"] == 0 and item["limit_up_precision"] is not None else "partial" if item["directional_evaluable_count"] else "unavailable"
        item["evaluation_reasons"] = (
            (["formal_ranked_predictions_unavailable"] if not item["sample_count"] else [])
            + (["candidate_outcome_bar_missing_or_invalid"] if item["directional_unknown_count"] else [])
            + (["outcome_limit_up_pool_missing"] if item["limit_up_evaluable_count"] < item["sample_count"] else [])
        )
    for item in finalized.values():
        item["limit_up_lift"] = _promotion_review_ratio(
            item["limit_up_precision"],
            baseline["limit_up_precision"],
        )
        item["directional_lift"] = _promotion_review_ratio(
            item["directional_precision"],
            baseline["directional_precision"],
        )
        item["strong_rise_lift"] = _promotion_review_ratio(
            item["strong_rise_precision"],
            baseline["strong_rise_precision"],
        )
        for metric in ("limit_up", "directional", "strong_rise"):
            if item[metric + "_precision"] is None or baseline[metric + "_precision"] in (None, 0):
                item[metric + "_lift"] = None
    return finalized


def _build_promotion_launch_precursor_metrics(
    records: list[PromotionPredictionRecord],
    *,
    limit_up_codes: set[str],
    rising_codes: set[str],
    strong_rising_codes: set[str],
    evaluable_codes: set[str] | None = None,
    limit_up_available: bool = True,
) -> dict[str, dict]:
    raw_metrics: dict[str, dict] = defaultdict(lambda: defaultdict(int))
    for record in records:
        code = str(record.code or "").strip()
        if not code:
            continue
        factors = _json_loads_safe(record.factors_json)
        actionable = factors.get("prediction_actionable") is True
        for cohort in _promotion_launch_precursor_cohorts(factors):
            item = raw_metrics[cohort]
            item["sample_count"] += 1
            item["directional_evaluable_count"] += int(evaluable_codes is not None and code in evaluable_codes)
            item["limit_up_evaluable_count"] += int(limit_up_available)
            item["actionable_limit_up_evaluable_count"] += int(limit_up_available and actionable)
            if code in limit_up_codes:
                item["limit_up_hit_count"] += 1
            if code in rising_codes:
                item["rise_hit_count"] += 1
            if code in strong_rising_codes:
                item["strong_rise_hit_count"] += 1
            if actionable:
                item["actionable_count"] += 1
                if code in limit_up_codes:
                    item["actionable_limit_up_hit_count"] += 1
    return _promotion_finalize_launch_precursor_metrics(raw_metrics)


def _aggregate_promotion_launch_precursor_metrics(daily_rows: list[dict]) -> dict[str, dict]:
    raw_metrics: dict[str, dict] = defaultdict(lambda: defaultdict(int))
    count_fields = (
        "sample_count",
        "limit_up_hit_count",
        "rise_hit_count",
        "strong_rise_hit_count",
        "actionable_count",
        "actionable_limit_up_hit_count",
        "directional_evaluable_count",
        "limit_up_evaluable_count",
        "actionable_limit_up_evaluable_count",
    )
    for row in daily_rows:
        for cohort, metrics in (row.get("launch_precursor_metrics") or {}).items():
            if cohort not in PROMOTION_LAUNCH_COHORT_LABELS:
                continue
            for field in count_fields:
                raw_metrics[cohort][field] += _safe_int(metrics.get(field))
    finalized = _promotion_finalize_launch_precursor_metrics(raw_metrics)
    for cohort, metrics in finalized.items():
        source_reasons = {
            reason for row in daily_rows
            for reason in ((row.get("launch_precursor_metrics") or {}).get(cohort) or {}).get("evaluation_reasons", [])
        }
        if "prediction_limit_up_pool_missing" in source_reasons:
            metrics["evaluation_reasons"] = sorted(
                (set(metrics["evaluation_reasons"]) - {"outcome_limit_up_pool_missing"}) | source_reasons
            )
    return finalized


def _promotion_review_recommendation(aggregate: dict) -> dict:
    # A mixed window is not a valid zero-precision sample. Stop before numeric
    # defaults can turn unknown outcomes into expansion/reranking advice.
    if (aggregate.get("evaluation_status") in {"partial", "unavailable"}
            or any(aggregate.get(field) is None for field in ("limit_up_precision", "limit_up_recall"))):
        return {
            "status": "data_incomplete",
            "label": "数据不完整，先核验复盘证据",
            "headline": "当前窗口含缺失或不可评估结果；未知不是失败，暂不据此建议扩池、重排或调整校准强度。",
            "actions": [
                "先核对正式收盘名单、记录交易日历、次日涨停池与正式收盘K线覆盖",
                "补齐可验证证据后重新复盘；不回补旧预测，也不将有效子集指标冒充全窗口成绩",
            ],
            "auto_policy": "本次复盘不生成扩池、排序权重或阈值调整建议",
        }
    valid_days = _safe_int(aggregate.get("valid_review_days"))
    recall = _safe_float(aggregate.get("limit_up_recall"))
    precision = _safe_float(aggregate.get("limit_up_precision"))
    not_in_pool = _safe_int(aggregate.get("not_in_pool_count"))
    rank_cutoff = _safe_int(aggregate.get("rank_cutoff_count"))
    score_low = _safe_int(aggregate.get("score_low_count"))
    overconcentrated_days = _safe_int(aggregate.get("overconcentrated_days"))
    rerank_missed = rank_cutoff + score_low
    missed_total = max(not_in_pool + rerank_missed, 1)
    calibration_bias = _safe_float(aggregate.get("calibration_bias"))
    review_window_label = f"近{valid_days}个有效复盘日" if valid_days else "当前复盘窗口"

    if valid_days < 3:
        status = "insufficient_sample"
        label = "样本不足，暂不自动调阈值"
        headline = "先保证每天盘后快照和实际结果都完整，再讨论放宽或收紧。"
    elif not_in_pool / missed_total >= 0.5 and recall < 0.12:
        status = "expand_feature_coverage"
        label = "优先补召回特征"
        headline = (
            f"{review_window_label}有 {not_in_pool} 只实际首/二板前一日未进入预测池；"
            "应补消息、竞价、板块扩散和静默蓄势覆盖，而不是直接降低买点门槛。"
        )
    elif rerank_missed / missed_total >= 0.5 and recall < 0.18:
        status = "rerank_missed_candidates"
        label = "优先重排已入池漏选票"
        headline = (
            f"{review_window_label}有 {rerank_missed} 只实际首/二板已入池但低分或被截断；"
            "应提高命中过的路线/形态排序权重，不需要无边界扩池。"
        )
    elif precision < 0.04 and recall < 0.12:
        status = "repair_signal_quality"
        label = "先修信号质量"
        headline = (
            f"{review_window_label}的精度和召回同时偏低，说明不是单纯阈值问题；"
            "应先排查数据时点、概率口径和特征失真。"
        )
    else:
        status = "stable_calibration"
        label = "保持保守校准"
        headline = (
            f"{review_window_label}进入稳定复盘阶段，继续按多日样本做概率截距校准，"
            "不用单日结果追着改规则。"
        )

    actions: list[str] = []
    if not_in_pool:
        actions.append(f"复核 {not_in_pool} 个未入池涨停样本，按消息/竞价/主线扩散/静默形态归类补特征")
    if rank_cutoff or score_low:
        actions.append(f"复核 {rank_cutoff + score_low} 个已入池但低分或被截断样本，比较命中票与假阳性差异")
    if overconcentrated_days:
        actions.append(
            f"最近窗口有 {overconcentrated_days} 天首板主榜过度集中于单一板块/主题；"
            "收盘预测保持分散，只有直接硬消息或竞价成交完整的题材集群允许提高暴露"
        )
    if calibration_bias >= 0.05:
        actions.append("预测概率明显高于实际命中率，继续以路线历史后验率为中心收缩，但保留个股相对排序")
    elif calibration_bias <= -0.03:
        actions.append("预测概率偏保守，只允许在累计样本充分后小幅上修，不因单日普涨放宽")
    actions.append("每日同时记录上涨、强涨和涨停三层结果，避免只用涨停一个标签误判趋势信号")
    return {
        "status": status,
        "label": label,
        "headline": headline,
        "actions": actions,
        "auto_policy": "只自动校准路线后验概率；选股阈值仅生成建议，不根据单日结果自改",
    }


async def _build_promotion_daily_learning_review(
    db: AsyncSession,
    *,
    lookback_days: int = PROMOTION_REVIEW_DEFAULT_DAYS,
    now: datetime | None = None,
) -> dict:
    """按交易日对齐预测快照与次日真实行情，生成可审计的精度/召回率成绩单。"""
    current = now or datetime.now()
    review_days = max(3, min(_safe_int(lookback_days, PROMOTION_REVIEW_DEFAULT_DAYS), PROMOTION_REVIEW_MAX_DAYS))
    calendar = dict((await db.execute(select(
        TradeCalendarModel.trade_date, TradeCalendarModel.is_trade_day
    ).where(TradeCalendarModel.trade_date <= current.date()))).all())
    ordered_dates = sorted((
        day for day, opened in calendar.items()
        if opened and _is_completed_promotion_outcome_date(day, now=current)
    ), reverse=True)
    recorded_dates = (await db.execute(select(
        PromotionPredictionRecord.prediction_trade_date
    ).where(PromotionPredictionRecord.prediction_trade_date < current.date()).distinct()
      .order_by(desc(PromotionPredictionRecord.prediction_trade_date)).limit(review_days))).scalars().all()
    prediction_dates = list(recorded_dates) if recorded_dates else ordered_dates[1:review_days + 1]
    review_pairs = []
    for prediction_day in prediction_dates:
        outcome_day, reason = next_recorded_trade_day(prediction_day, calendar, through=current.date())
        if outcome_day is not None and not _is_completed_promotion_outcome_date(outcome_day, now=current):
            continue
        review_pairs.append((prediction_day, outcome_day, reason))
    actual_dates = [day for _, day, _ in review_pairs if day is not None]
    pool_dates = set((await db.execute(select(LimitUpPool.trade_date).where(
        LimitUpPool.trade_date.in_(actual_dates + prediction_dates), LimitUpPool.quarantined.is_(False)
    ).distinct())).scalars().all())

    actual_limit_ups_by_date: dict[date, list[dict]] = {}
    market_limit_up_counts_by_date: dict[date, int] = {}
    for actual_trade_date in actual_dates:
        market_limit_ups = await _load_filtered_limit_ups(db, actual_trade_date)
        market_limit_up_counts_by_date[actual_trade_date] = sum(
            1
            for item in market_limit_ups
            if _safe_int(item.get("consecutive_days"), 1) in {1, 2}
        )
        actual_limit_ups_by_date[actual_trade_date] = [
            item
            for item in market_limit_ups
            if stock_tagger.is_tradeable(str(item.get("code") or "").strip())
        ]

    kline_rows = (await db.execute(select(StockKline).where(
        StockKline.trade_date.in_(actual_dates + prediction_dates)
    ))).scalars().all()
    bars = {(bar.code, bar.trade_date): bar for bar in kline_rows}

    prediction_result = await db.execute(
        select(PromotionPredictionRecord).where(
            PromotionPredictionRecord.prediction_trade_date.in_(prediction_dates),
            PromotionPredictionRecord.target_board.in_([1, 2]),
        )
    )
    latest_prediction_records = [
        record
        for record in _latest_learning_batch_records(list(prediction_result.scalars().all()))
        if not _is_legacy_observation_first_board_record(record)
    ]
    records_by_date_target: dict[tuple[date, int], list[PromotionPredictionRecord]] = defaultdict(list)
    for record in latest_prediction_records:
        factors = _json_loads_safe(record.factors_json)
        if _prediction_record_snapshot_source(record, factors) != "schedule":
            continue
        records_by_date_target[(record.prediction_trade_date, _safe_int(record.target_board))].append(record)

    daily_rows: list[dict] = []
    aggregate_brier_sum = 0.0
    aggregate_brier_count = 0
    aggregate_probability_sum = 0.0
    aggregate_outcome_sum = 0
    for prediction_trade_date, actual_trade_date, calendar_reason in review_pairs:
        calendar_gap_days = (actual_trade_date - prediction_trade_date).days if actual_trade_date else 0
        limit_up_available = prediction_trade_date in pool_dates and actual_trade_date in pool_dates
        pool_reasons = []
        if prediction_trade_date not in pool_dates:
            pool_reasons.append("prediction_limit_up_pool_missing")
        if actual_trade_date not in pool_dates:
            pool_reasons.append("outcome_limit_up_pool_missing")
        evaluation_reasons = ([calendar_reason] if calendar_reason else []) + pool_reasons
        evaluable_codes = set()
        rising_codes = set()
        strong_rising_codes = set()
        bar_errors = {}
        for (code, day), after in bars.items():
            if actual_trade_date is None or day != actual_trade_date:
                continue
            error = formal_outcome_bar_error(bars.get((code, prediction_trade_date)), after)
            if not error and (not isinstance(after.change_pct, (int, float)) or not isfinite(after.change_pct)):
                error = "candidate_outcome_change_invalid"
            if error:
                bar_errors[code] = error
                continue
            evaluable_codes.add(code)
            change = (after.close / after.prev_close - 1) * 100
            if change > 0 and stock_tagger.is_tradeable(code):
                rising_codes.add(code)
            if change >= 5 and stock_tagger.is_tradeable(code):
                strong_rising_codes.add(code)
        # The shared normalizer can bridge absent pool dates. Without both
        # endpoint pools its first/second-board classification is unknown.
        actual_limit_ups = (actual_limit_ups_by_date.get(actual_trade_date) or []) if limit_up_available else []
        actual_codes_by_target = {
            1: {
                str(item.get("code") or "").strip()
                for item in actual_limit_ups
                if _safe_int(item.get("consecutive_days"), 1) == 1
            },
            2: {
                str(item.get("code") or "").strip()
                for item in actual_limit_ups
                if _safe_int(item.get("consecutive_days"), 1) == 2
            },
        }
        lane_metrics: dict[str, dict] = {}
        predicted_codes: set[str] = set()
        prediction_model_versions: set[str] = set()
        prediction_snapshot_contexts: set[str] = set()
        launch_precursor_metrics = _promotion_finalize_launch_precursor_metrics({})
        missed_reason_counts: dict[str, int] = defaultdict(int)
        missed_examples: list[dict] = []
        snapshot_complete = True
        day_brier_sum = 0.0
        day_brier_count = 0
        day_probability_sum = 0.0
        day_outcome_sum = 0
        predicted_sector_counts: dict[str, int] = defaultdict(int)
        predicted_theme_counts: dict[str, int] = defaultdict(int)
        predicted_unconfirmed_count = 0

        for target_board in (1, 2):
            lane_records = records_by_date_target.get((prediction_trade_date, target_board), [])
            best_records: dict[str, PromotionPredictionRecord] = {}
            for record in lane_records:
                code = str(record.code or "").strip()
                selected = _select_best_prediction_record([item for item in (best_records.get(code), record) if item is not None])
                if selected is not None:
                    best_records[code] = selected
            for record in best_records.values():
                factors = _json_loads_safe(record.factors_json)
                model_version = str(
                    record.model_version
                    or factors.get("prediction_model_version")
                    or ""
                ).strip()
                snapshot_context = _prediction_record_snapshot_context(record, factors)
                if model_version:
                    prediction_model_versions.add(model_version)
                if snapshot_context:
                    prediction_snapshot_contexts.add(snapshot_context)
            ranked_records = [
                record
                for record in best_records.values()
                if _json_loads_safe(record.factors_json).get("prediction_ranked_selected") is True
            ]
            ranked_records.sort(
                key=lambda record: _safe_int(_json_loads_safe(record.factors_json).get("prediction_ranked_position"), 9999)
            )
            ranked_codes = {str(record.code or "").strip() for record in ranked_records}
            pool_codes = {
                str(record.code or "").strip()
                for record in best_records.values()
                if str(record.code or "").strip()
            }
            recall_ranked_available = any(
                "prediction_recall_ranked_selected" in _json_loads_safe(record.factors_json)
                for record in best_records.values()
            )
            recall_ranked_records = [
                record
                for record in best_records.values()
                if _json_loads_safe(record.factors_json).get(
                    "prediction_recall_ranked_selected"
                ) is True
            ]
            recall_ranked_records.sort(
                key=lambda record: _safe_int(
                    _json_loads_safe(record.factors_json).get(
                        "prediction_recall_ranked_position"
                    ),
                    9999,
                )
            )
            recall_ranked_codes = {
                str(record.code or "").strip()
                for record in recall_ranked_records
                if str(record.code or "").strip()
            }
            predicted_codes.update(ranked_codes)
            if target_board == 1:
                for record in ranked_records:
                    factors = _json_loads_safe(record.factors_json)
                    exposure_item = {
                        "sector_name": str(factors.get("sector_name") or ""),
                        "sector_code": str(factors.get("sector_code") or ""),
                        "probability_factors": factors,
                    }
                    sector_key = _first_board_rank_sector_key(exposure_item)
                    theme_key, _theme_label = _first_board_rank_broad_theme(exposure_item)
                    if sector_key:
                        predicted_sector_counts[sector_key] += 1
                    if theme_key:
                        predicted_theme_counts[theme_key] += 1
                    if not _first_board_rank_has_active_confirmation(exposure_item):
                        predicted_unconfirmed_count += 1
            actual_codes = actual_codes_by_target[target_board]
            hit_codes = ranked_codes & actual_codes
            pool_hit_codes = pool_codes & actual_codes
            recall_hit_codes = recall_ranked_codes & actual_codes
            actionability_labeled_records = [
                record
                for record in ranked_records
                if "prediction_actionable" in _json_loads_safe(record.factors_json)
            ]
            actionable_records = [
                record
                for record in actionability_labeled_records
                if _json_loads_safe(record.factors_json).get("prediction_actionable") is True
            ]
            actionable_codes = {
                str(record.code or "").strip()
                for record in actionable_records
                if str(record.code or "").strip()
            }
            actionable_hit_codes = actionable_codes & actual_codes
            if target_board == 1:
                launch_precursor_metrics = _build_promotion_launch_precursor_metrics(
                    ranked_records,
                    limit_up_codes=actual_codes,
                    rising_codes=rising_codes,
                    strong_rising_codes=strong_rising_codes,
                    evaluable_codes=evaluable_codes,
                    limit_up_available=limit_up_available,
                )
            low_probability_cutoff = 0.10 if target_board == 1 else 0.08
            underestimated_hit_count = sum(
                1
                for record in ranked_records
                if str(record.code or "").strip() in hit_codes
                and _safe_float(
                    record.calibrated_probability,
                    _safe_float(record.predicted_probability),
                ) < low_probability_cutoff
            )
            minimum_records = (
                PROMOTION_MIN_FIRST_BOARD_SNAPSHOT_RECORDS
                if target_board == 1
                else PROMOTION_MIN_SECOND_BOARD_SNAPSHOT_RECORDS
            )
            lane_snapshot_complete = len(best_records) >= minimum_records
            snapshot_complete = snapshot_complete and lane_snapshot_complete

            for actual_code in actual_codes:
                record = best_records.get(actual_code)
                status, reason, _detail = _resolve_actual_replay_status(
                    code=actual_code,
                    target_board=target_board,
                    record=record,
                    tag=None,
                    snapshot_incomplete=not lane_snapshot_complete,
                    snapshot_record_count=len(best_records),
                    snapshot_min_record_count=minimum_records,
                )
                missed_reason_counts[status] += 1
                if status != "predicted_hit" and len(missed_examples) < 12:
                    actual_item = next(
                        (item for item in actual_limit_ups if str(item.get("code") or "").strip() == actual_code),
                        {},
                    )
                    missed_examples.append(
                        {
                            "code": actual_code,
                            "name": str(actual_item.get("name") or (record.name if record else "") or ""),
                            "target_board": target_board,
                            "target_label": "首板" if target_board == 1 else "二板",
                            "status": status,
                            "reason": reason,
                            "predicted_probability": _safe_float(
                                record.calibrated_probability,
                                _safe_float(record.predicted_probability),
                            ) if record else 0.0,
                            "candidate_route": str(record.candidate_route or "") if record else "",
                        }
                    )

            for record in ranked_records:
                if not limit_up_available:
                    continue
                probability = _safe_float(record.calibrated_probability, _safe_float(record.predicted_probability))
                outcome = 1 if str(record.code or "").strip() in actual_codes else 0
                day_brier_sum += (probability - outcome) ** 2
                day_brier_count += 1
                day_probability_sum += probability
                day_outcome_sum += outcome

            lane_metrics[f"target_{target_board}"] = {
                "target_label": "首板" if target_board == 1 else "二板",
                **_promotion_directional_metrics(len(ranked_codes), len(ranked_codes & evaluable_codes), len(ranked_codes & rising_codes)),
                "actual_count": len(actual_codes),
                "pool_count": len(pool_codes),
                "pool_hit_count": len(pool_hit_codes),
                "pool_recall": _promotion_review_ratio(
                    len(pool_hit_codes),
                    len(actual_codes),
                ),
                "predicted_count": len(ranked_records),
                "hit_count": len(hit_codes),
                "underestimated_hit_count": underestimated_hit_count,
                "precision": _promotion_review_ratio(len(hit_codes), len(ranked_records)),
                "recall": _promotion_review_ratio(len(hit_codes), len(actual_codes)),
                "recall_ranked_available": recall_ranked_available,
                "recall_ranked_count": len(recall_ranked_codes),
                "recall_hit_count": len(recall_hit_codes),
                "recall_precision": _promotion_review_ratio(
                    len(recall_hit_codes),
                    len(recall_ranked_codes),
                ),
                "recall_recall": _promotion_review_ratio(
                    len(recall_hit_codes),
                    len(actual_codes),
                ),
                "actionability_labeled_count": len(actionability_labeled_records),
                "actionable_predicted_count": len(actionable_codes),
                "actionable_hit_count": len(actionable_hit_codes),
                "actionable_precision": _promotion_review_ratio(
                    len(actionable_hit_codes),
                    len(actionable_codes),
                ),
                "forecast_only_count": max(
                    len(actionability_labeled_records) - len(actionable_codes),
                    0,
                ),
                "snapshot_complete": lane_snapshot_complete,
            }

        for lane in lane_metrics.values():
            lane["evaluation_reasons"] = (
                (["snapshot_incomplete"] if not lane["snapshot_complete"] else [])
                + (["candidate_outcome_bar_missing_or_invalid"] if lane["directional_unknown_count"] else [])
                + pool_reasons
                + (["formal_ranked_predictions_unavailable"] if not lane["predicted_count"] else [])
            )
            lane["evaluation_status"] = "complete" if not lane["evaluation_reasons"] else "partial" if lane["predicted_count"] else "unavailable"
            if not limit_up_available:
                for field in ("actual_count", "pool_hit_count", "pool_recall", "hit_count", "underestimated_hit_count",
                              "precision", "recall", "recall_hit_count", "recall_precision", "recall_recall",
                              "actionable_hit_count", "actionable_precision"):
                    lane[field] = None
        actual_target_codes = actual_codes_by_target[1] | actual_codes_by_target[2]
        total_hits = sum(_safe_int(item.get("hit_count")) for item in lane_metrics.values())
        total_actionable_predictions = sum(
            _safe_int(item.get("actionable_predicted_count"))
            for item in lane_metrics.values()
        )
        total_actionable_hits = sum(
            _safe_int(item.get("actionable_hit_count"))
            for item in lane_metrics.values()
        )
        underestimated_hits = sum(
            _safe_int(item.get("underestimated_hit_count"))
            for item in lane_metrics.values()
        )
        top_sector_key, top_sector_count = max(
            predicted_sector_counts.items(),
            key=lambda pair: pair[1],
            default=("", 0),
        )
        top_theme_key, top_theme_count = max(
            predicted_theme_counts.items(),
            key=lambda pair: pair[1],
            default=("", 0),
        )
        first_board_predicted_count = _safe_int((lane_metrics.get("target_1") or {}).get("predicted_count"))
        daily_rows.append(
            {
                "actual_trade_date": str(actual_trade_date) if actual_trade_date else None,
                "prediction_trade_date": str(prediction_trade_date),
                "evaluation_scope": "previous_close_schedule_snapshot",
                "calendar_gap_days": calendar_gap_days,
                "has_unobservable_news_window": calendar_gap_days > 1,
                "evaluation_scope_warning": (
                    "跨周末/节假日：该成绩只考核上一交易日收盘快照，期间新增公告与消息应由09:25/09:35盘前快照另行考核"
                    if calendar_gap_days > 1
                    else "该成绩考核上一交易日收盘快照；当日09:25/09:35确认属于独立时点"
                ),
                "snapshot_complete": snapshot_complete,
                "prediction_model_version": (
                    " / ".join(sorted(prediction_model_versions))
                    if prediction_model_versions
                    else None
                ),
                "prediction_model_versions": sorted(prediction_model_versions),
                "prediction_snapshot_contexts": sorted(prediction_snapshot_contexts),
                "actual_rising_count": len(rising_codes),
                "actual_strong_rising_count": len(strong_rising_codes),
                "market_target_limit_up_count": market_limit_up_counts_by_date.get(actual_trade_date, 0),
                "actual_target_limit_up_count": len(actual_target_codes),
                "predicted_count": len(predicted_codes),
                "predicted_rising_hit_count": len(predicted_codes & rising_codes),
                "predicted_strong_rising_hit_count": len(predicted_codes & strong_rising_codes),
                "predicted_limit_up_hit_count": total_hits,
                "underestimated_limit_up_hit_count": underestimated_hits,
                "actionable_predicted_count": total_actionable_predictions,
                "actionable_limit_up_hit_count": total_actionable_hits,
                "actionable_limit_up_precision": _promotion_review_ratio(
                    total_actionable_hits,
                    total_actionable_predictions,
                ),
                "directional_precision": _promotion_review_ratio(len(predicted_codes & rising_codes), len(predicted_codes)),
                "strong_rise_precision": _promotion_review_ratio(len(predicted_codes & strong_rising_codes), len(predicted_codes)),
                "limit_up_precision": _promotion_review_ratio(total_hits, len(predicted_codes)),
                "limit_up_recall": _promotion_review_ratio(total_hits, len(actual_target_codes)),
                "average_predicted_probability": _promotion_review_ratio(day_probability_sum, day_brier_count),
                "observed_hit_rate": _promotion_review_ratio(day_outcome_sum, day_brier_count),
                "calibration_bias": round(
                    _promotion_review_ratio(day_probability_sum, day_brier_count)
                    - _promotion_review_ratio(day_outcome_sum, day_brier_count),
                    4,
                ),
                "brier_score": round(day_brier_sum / day_brier_count, 4) if day_brier_count else 0.0,
                "missed_reason_counts": dict(missed_reason_counts),
                "missed_examples": missed_examples,
                "lane_metrics": lane_metrics,
                "launch_precursor_metrics": launch_precursor_metrics,
                "prediction_exposure": {
                    "first_board_predicted_count": first_board_predicted_count,
                    "unique_sector_count": len(predicted_sector_counts),
                    "unique_theme_count": len(predicted_theme_counts),
                    "top_sector_key": top_sector_key,
                    "top_sector_count": top_sector_count,
                    "top_sector_share": _promotion_review_ratio(top_sector_count, first_board_predicted_count),
                    "top_theme_key": top_theme_key,
                    "top_theme_count": top_theme_count,
                    "top_theme_share": _promotion_review_ratio(top_theme_count, first_board_predicted_count),
                    "unconfirmed_count": predicted_unconfirmed_count,
                    "unconfirmed_share": _promotion_review_ratio(predicted_unconfirmed_count, first_board_predicted_count),
                    "overconcentrated": bool(
                        first_board_predicted_count >= 5
                        and (top_sector_count >= 3 or top_theme_count >= 4)
                    ),
                },
            }
        )
        row = daily_rows[-1]
        if pool_reasons:
            for cohort in launch_precursor_metrics.values():
                cohort["evaluation_reasons"] = sorted(set(
                    [reason for reason in cohort["evaluation_reasons"] if reason != "outcome_limit_up_pool_missing"]
                    + pool_reasons
                ))
        row.update(_promotion_directional_metrics(len(predicted_codes), len(predicted_codes & evaluable_codes), len(predicted_codes & rising_codes)))
        if row["directional_precision"] is None:
            row["strong_rise_precision"] = None
        for code in sorted(predicted_codes - evaluable_codes):
            evaluation_reasons.append(bar_errors.get(code, "candidate_outcome_bar_missing"))
        if not snapshot_complete:
            evaluation_reasons.append("snapshot_incomplete")
        if not predicted_codes:
            evaluation_reasons.append("formal_ranked_predictions_unavailable")
        row["evaluation_reasons"] = sorted(set(evaluation_reasons))
        row["evaluation_status"] = ("complete" if not evaluation_reasons else
                                    "partial" if predicted_codes and (limit_up_available or row["directional_evaluable_count"]) else "unavailable")
        row["limit_up_evaluable_count"] = len(predicted_codes) if limit_up_available else 0
        if not predicted_codes:
            for field in ("limit_up_precision", "actionable_limit_up_precision", "brier_score", "observed_hit_rate", "calibration_bias"):
                row[field] = None
        if not limit_up_available:
            for field in ("market_target_limit_up_count", "actual_target_limit_up_count", "predicted_limit_up_hit_count",
                          "underestimated_limit_up_hit_count", "actionable_limit_up_hit_count", "actionable_limit_up_precision",
                          "limit_up_precision", "limit_up_recall", "observed_hit_rate", "calibration_bias", "brier_score"):
                row[field] = None
        aggregate_brier_sum += day_brier_sum
        aggregate_brier_count += day_brier_count
        aggregate_probability_sum += day_probability_sum
        aggregate_outcome_sum += day_outcome_sum

    aggregate = {
        "review_days": len(daily_rows),
        "valid_review_days": sum(1 for item in daily_rows if item.get("evaluation_status") == "complete"),
        "actual_rising_count": sum(_safe_int(item.get("actual_rising_count")) for item in daily_rows),
        "actual_strong_rising_count": sum(_safe_int(item.get("actual_strong_rising_count")) for item in daily_rows),
        "market_target_limit_up_count": sum(_safe_int(item.get("market_target_limit_up_count")) for item in daily_rows),
        "actual_target_limit_up_count": sum(_safe_int(item.get("actual_target_limit_up_count")) for item in daily_rows),
        "predicted_count": sum(_safe_int(item.get("predicted_count")) for item in daily_rows),
        "predicted_rising_hit_count": sum(_safe_int(item.get("predicted_rising_hit_count")) for item in daily_rows),
        "predicted_strong_rising_hit_count": sum(_safe_int(item.get("predicted_strong_rising_hit_count")) for item in daily_rows),
        "predicted_limit_up_hit_count": sum(_safe_int(item.get("predicted_limit_up_hit_count")) for item in daily_rows),
        "actionable_predicted_count": sum(
            _safe_int(item.get("actionable_predicted_count"))
            for item in daily_rows
        ),
        "actionable_limit_up_hit_count": sum(
            _safe_int(item.get("actionable_limit_up_hit_count"))
            for item in daily_rows
        ),
        "underestimated_limit_up_hit_count": sum(
            _safe_int(item.get("underestimated_limit_up_hit_count")) for item in daily_rows
        ),
        "not_in_pool_count": sum(_safe_int((item.get("missed_reason_counts") or {}).get("not_in_pool")) for item in daily_rows),
        "score_low_count": sum(_safe_int((item.get("missed_reason_counts") or {}).get("score_low")) for item in daily_rows),
        "rank_cutoff_count": sum(_safe_int((item.get("missed_reason_counts") or {}).get("rank_cutoff")) for item in daily_rows),
        "snapshot_incomplete_count": sum(_safe_int((item.get("missed_reason_counts") or {}).get("snapshot_incomplete")) for item in daily_rows),
        "overconcentrated_days": sum(
            1 for item in daily_rows if bool((item.get("prediction_exposure") or {}).get("overconcentrated"))
        ),
        "brier_score": round(aggregate_brier_sum / aggregate_brier_count, 4) if aggregate_brier_count else 0.0,
        "average_predicted_probability": _promotion_review_ratio(aggregate_probability_sum, aggregate_brier_count),
        "observed_hit_rate": _promotion_review_ratio(aggregate_outcome_sum, aggregate_brier_count),
    }
    aggregate["launch_precursor_metrics"] = _aggregate_promotion_launch_precursor_metrics(daily_rows)
    aggregate["directional_precision"] = _promotion_review_ratio(
        aggregate["predicted_rising_hit_count"], aggregate["predicted_count"]
    )
    aggregate["strong_rise_precision"] = _promotion_review_ratio(
        aggregate["predicted_strong_rising_hit_count"], aggregate["predicted_count"]
    )
    aggregate["limit_up_precision"] = _promotion_review_ratio(
        aggregate["predicted_limit_up_hit_count"], aggregate["predicted_count"]
    )
    aggregate["actionable_limit_up_precision"] = _promotion_review_ratio(
        aggregate["actionable_limit_up_hit_count"],
        aggregate["actionable_predicted_count"],
    )
    aggregate["limit_up_recall"] = _promotion_review_ratio(
        aggregate["predicted_limit_up_hit_count"], aggregate["actual_target_limit_up_count"]
    )
    aggregate["calibration_bias"] = round(
        _safe_float(aggregate.get("average_predicted_probability"))
        - _safe_float(aggregate.get("observed_hit_rate")),
        4,
    )
    aggregate.update(_promotion_directional_metrics(
        aggregate["predicted_count"],
        sum(row["directional_evaluable_count"] for row in daily_rows),
        aggregate["predicted_rising_hit_count"],
    ))
    aggregate["evaluation_reasons"] = sorted({reason for row in daily_rows for reason in row["evaluation_reasons"]})
    aggregate["evaluation_status"] = ("complete" if daily_rows and aggregate["valid_review_days"] == len(daily_rows)
                                       else "partial" if aggregate["predicted_count"] else "unavailable")
    aggregate["limit_up_evaluable_count"] = sum(row["limit_up_evaluable_count"] for row in daily_rows)
    if aggregate["directional_precision"] is None:
        aggregate["strong_rise_precision"] = None
    if not daily_rows or any(row["limit_up_precision"] is None for row in daily_rows):
        for field in ("limit_up_precision", "limit_up_recall", "actionable_limit_up_precision",
                      "observed_hit_rate", "calibration_bias", "brier_score",
                      "predicted_limit_up_hit_count", "actual_target_limit_up_count", "market_target_limit_up_count"):
            aggregate[field] = None
    recommendation = _promotion_review_recommendation(aggregate)
    return {
        "status": "ok" if daily_rows else "insufficient_data",
        "evaluation_as_of": current.isoformat(timespec="seconds"),
        "latest_completed_trade_date": str(ordered_dates[0]) if ordered_dates else None,
        "lookback_days": review_days,
        "latest": daily_rows[0] if daily_rows else {},
        "aggregate": aggregate,
        "daily": daily_rows,
        "recommendation": recommendation,
        "notes": [
            "复盘读取兼容 record 与当前正式收盘行情；不是不可变预测/结果证据，不代表满足治理晋级或可上线",
            "方向精度固定正式名单分母；缺K/无效K为unknown，observed_precision仅是已评价子集，缺失时全名单precision与target_met为null",
            "交易日按记录日历严格T+1；缺涨停池不生成整日负标签，空池与未采集尚无完整性证明时保守未知",
            "预测数只统计前一交易日 schedule 快照中 ranked_selected=true 的正式主榜，不把页面补位观察股冒充预测",
            "lane_metrics 将 pool_recall、正式 Top12 和 Top30 宽召回成绩分开，首板/二板也分别统计，不能把候选池覆盖或二板命中冒充首板精度",
            "prediction_actionable 单独统计当时已通过交易执行闸门的子集；正式预测命中与可执行交易命中不得混成一个精度",
            "跨周末/节假日的消息在上一交易日收盘时尚不可见，daily.has_unobservable_news_window 会明确标记，不能据此把周末突发催化算作收盘模型漏选",
            "实际结果同时统计全市场首/二板数和主板上涨、强涨(>=5%)、首板、二板；模型精度/召回只按可交易主板目标严格对齐",
            "每天的漏选按未入池、分数低、排序截断、快照不完整分开归因",
            "prediction_exposure 记录首板主榜的板块/宽主题集中度；预测榜不再硬删题材簇，集中风险只作用于独立交易执行闸门",
            "launch_precursor_metrics 分组跟踪中低位修复、三日资金、主营行业点火和直接消息的上涨/强涨/首板表现；各组可重叠且不会把预测证据当作交易闸门",
            "自动学习以路线历史后验命中率为中心收缩预测，并保留部分个股相对排序；阈值修改需多日样本验证",
        ],
    }


async def run_promotion_daily_learning_review(
    db: AsyncSession,
    *,
    lookback_days: int = PROMOTION_REVIEW_DEFAULT_DAYS,
) -> dict:
    evaluated_records = await _refresh_promotion_learning(db)
    if evaluated_records:
        await db.commit()
    payload = await _build_promotion_daily_learning_review(db, lookback_days=lookback_days)
    payload["evaluated_records"] = evaluated_records
    cache_scope = _promotion_cache_scope(db)
    for cache_key in list(_PROMOTION_LEARNING_REVIEW_CACHE):
        if cache_key[:2] == cache_scope:
            _PROMOTION_LEARNING_REVIEW_CACHE.pop(cache_key, None)
    return payload


async def _build_auction_snapshot_health(
    db: AsyncSession,
    trade_date: date,
) -> dict:
    latest_time_subquery = (
        select(
            AuctionData.code.label("code"),
            func.max(AuctionData.auction_time).label("latest_auction_time"),
        )
        .where(
            AuctionData.trade_date == trade_date,
            AuctionData.auction_time.between("09:15:00", "09:25:30"),
        )
        .group_by(AuctionData.code)
        .subquery()
    )
    result = await db.execute(
        select(AuctionData)
        .join(
            latest_time_subquery,
            and_(
                latest_time_subquery.c.code == AuctionData.code,
                latest_time_subquery.c.latest_auction_time == AuctionData.auction_time,
            ),
        )
        .where(AuctionData.trade_date == trade_date)
    )
    rows = list(result.scalars().all())
    latest_code_count = len(rows)
    price_complete_count = 0
    volume_complete_count = 0
    amount_complete_count = 0
    volume_ratio_complete_count = 0
    feed_complete_count = 0
    timely_snapshot_count = 0
    strong_open_count = 0
    executable_strong_open_count = 0
    overextended_open_count = 0
    latest_snapshot_time = ""
    decision_at = datetime.now()
    evidence_counts: dict[str, int] = {}
    for row in rows:
        auction_price, prev_close = row.auction_price, row.prev_close
        auction_volume, auction_amount = row.auction_volume, row.auction_amount
        volume_ratio, open_change = row.volume_ratio, row.open_change
        evidence_status = auction_evidence_status(row, decision_at=decision_at)
        evidence_counts[evidence_status] = evidence_counts.get(evidence_status, 0) + 1
        normalized_time = str(row.auction_time or "")
        latest_snapshot_time = max(latest_snapshot_time, normalized_time)
        timely_snapshot_count += int("09:24:00" <= normalized_time <= "09:25:30")
        price_complete = _safe_float(auction_price) > 0 and _safe_float(prev_close) > 0
        volume_complete = _safe_float(auction_volume) > 0
        amount_complete = _safe_float(auction_amount) > 0
        volume_ratio_complete = _safe_float(volume_ratio) > 0
        # Numeric display coverage is not source/phase/unit evidence.
        feed_complete = evidence_status == "ok"
        price_complete_count += int(price_complete)
        volume_complete_count += int(volume_complete)
        amount_complete_count += int(amount_complete)
        volume_ratio_complete_count += int(volume_ratio_complete)
        feed_complete_count += int(feed_complete)
        gap = _safe_float(open_change)
        strong_open = AUCTION_SURGE_MIN_OPEN_CHANGE <= gap < AUCTION_SURGE_MAX_OPEN_CHANGE
        strong_open_count += int(strong_open)
        executable_strong_open_count += int(strong_open and feed_complete)
        overextended_open_count += int(gap >= AUCTION_SURGE_MAX_OPEN_CHANGE)

    complete_ratio = _promotion_review_ratio(feed_complete_count, latest_code_count)
    timely_ratio = _promotion_review_ratio(timely_snapshot_count, latest_code_count)
    missing = latest_code_count <= 0
    stale = bool(not missing and timely_ratio < 0.80)
    field_degraded = bool(not missing and complete_ratio < 0.80)
    degraded = bool(field_degraded or stale)
    return {
        "trade_date": str(trade_date),
        "snapshot_count": latest_code_count,
        "latest_code_count": latest_code_count,
        "latest_snapshot_time": latest_snapshot_time,
        "timely_snapshot_count": timely_snapshot_count,
        "timely_snapshot_ratio": timely_ratio,
        "minimum_timely_ratio": 0.80,
        "missing": missing,
        "stale": stale,
        "field_degraded": field_degraded,
        "degraded": degraded,
        "status": (
            "missing"
            if missing
            else "stale"
            if stale
            else "degraded"
            if field_degraded
            else "ok"
        ),
        "expected_time": "09:24:00~09:25:30",
        "minimum_complete_ratio": 0.80,
        "price_complete_count": price_complete_count,
        "volume_complete_count": volume_complete_count,
        "amount_complete_count": amount_complete_count,
        "volume_ratio_complete_count": volume_ratio_complete_count,
        "feed_complete_count": feed_complete_count,
        "feed_complete_ratio": complete_ratio,
        "strong_open_count": strong_open_count,
        "executable_strong_open_count": executable_strong_open_count,
        "evidence_contract": "auction_provenance_v1",
        "evidence_status_counts": evidence_counts,
        "overextended_open_count": overextended_open_count,
    }


async def _build_prediction_snapshot_health(
    db: AsyncSession,
    *,
    first_board_trade_date: date,
    second_board_trade_date: date,
    actual_trade_date: date,
) -> dict:
    """检查分赛道、模型版本和竞价时点，避免旧快照冒充当前预测。"""
    expected_dates = list(dict.fromkeys([first_board_trade_date, second_board_trade_date]))
    expected_lane_keys = {
        (first_board_trade_date, 1),
        (second_board_trade_date, 2),
    }
    official_record_filter = or_(
        PromotionPredictionRecord.target_board != 1,
        PromotionPredictionRecord.candidate_route.is_(None),
        ~PromotionPredictionRecord.candidate_route.in_(list(FIRST_BOARD_LEGACY_OBSERVATION_RECORD_ROUTES)),
    )
    latest_record_date = (
        await db.execute(
            select(func.max(PromotionPredictionRecord.prediction_trade_date)).where(official_record_filter)
        )
    ).scalar_one_or_none()

    expected_records: list[PromotionPredictionRecord] = []
    if expected_dates:
        expected_result = await db.execute(
            select(PromotionPredictionRecord)
            .where(PromotionPredictionRecord.prediction_trade_date.in_(expected_dates))
            .where(official_record_filter)
        )
        expected_records = [
            record
            for record in expected_result.scalars().all()
            if not _is_legacy_observation_first_board_record(record)
        ]
    # 旧开发库没有显式 snapshot_source/model_version 时仍以 legacy 兼容；
    # 一旦记录声明了旧模型版本，就不能继续冒充当前版本的正式预测。
    official_records_by_lane: dict[tuple[date, int], list[PromotionPredictionRecord]] = defaultdict(list)
    context_counts: dict[str, dict[str, dict[str, int]]] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(int))
    )
    context_unique_codes: dict[str, dict[str, dict[str, set[str]]]] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(set))
    )
    model_version_counts: dict[str, dict[str, dict[str, int]]] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(int))
    )
    for record in expected_records:
        record_date = record.prediction_trade_date
        target_board = _safe_int(record.target_board)
        lane_key = (record_date, target_board)
        context = _prediction_record_snapshot_context(record) or "legacy"
        code = str(record.code or "").strip()
        version = _prediction_record_model_version(record) or "legacy"
        context_counts[str(record_date)][f"target_{target_board}"][context] += 1
        if code:
            context_unique_codes[str(record_date)][f"target_{target_board}"][context].add(code)
        model_version_counts[str(record_date)][f"target_{target_board}"][version] += 1
        raw_source = str(getattr(record, "snapshot_source", "") or "")
        if lane_key in expected_lane_keys and (
            _prediction_record_snapshot_source(record) == "schedule"
            or raw_source in {"", "legacy"}
        ):
            official_records_by_lane[lane_key].append(record)

    version_eligible_records: list[PromotionPredictionRecord] = []
    version_mismatch_lanes: list[dict] = []
    for (record_date, target_board), lane_records in official_records_by_lane.items():
        current_records = [
            record
            for record in lane_records
            if _prediction_record_model_version(record) == PROMOTION_MODEL_VERSION
        ]
        legacy_records = [
            record
            for record in lane_records
            if _prediction_record_model_version(record) == "legacy"
        ]
        explicit_old_records = [
            record
            for record in lane_records
            if _prediction_record_model_version(record) not in {PROMOTION_MODEL_VERSION, "legacy"}
        ]
        if current_records:
            version_eligible_records.extend(current_records)
        elif explicit_old_records:
            versions = sorted({_prediction_record_model_version(record) for record in explicit_old_records})
            version_mismatch_lanes.append(
                {
                    "trade_date": str(record_date),
                    "target_board": target_board,
                    "record_count": len(explicit_old_records),
                    "versions": versions,
                }
            )
        else:
            version_eligible_records.extend(legacy_records)

    canonical_records = _latest_learning_batch_records(version_eligible_records)
    canonical_code_sets: dict[tuple[date, int], set[str]] = defaultdict(set)
    raw_code_sets: dict[tuple[date, int], set[str]] = defaultdict(set)
    for lane_key, lane_records in official_records_by_lane.items():
        raw_code_sets[lane_key].update(
            str(record.code or "").strip()
            for record in lane_records
            if str(record.code or "").strip()
        )
    for record in canonical_records:
        code = str(record.code or "").strip()
        if code:
            canonical_code_sets[
                (record.prediction_trade_date, _safe_int(record.target_board))
            ].add(code)

    counts = {lane_key: len(codes) for lane_key, codes in canonical_code_sets.items()}
    raw_counts = {lane_key: len(codes) for lane_key, codes in raw_code_sets.items()}

    latest_trade_date_total = sum(
        count
        for (record_date, _target), count in counts.items()
        if record_date == second_board_trade_date
    )
    first_target_count = counts.get((first_board_trade_date, 1), 0)
    second_target_count = counts.get((second_board_trade_date, 2), 0)

    replay_prediction_trade_date = await _get_previous_limit_up_trade_date(db, actual_trade_date)
    replay_record_count = 0
    if replay_prediction_trade_date is not None:
        replay_result = await db.execute(
            select(PromotionPredictionRecord).where(
                PromotionPredictionRecord.prediction_trade_date == replay_prediction_trade_date,
                PromotionPredictionRecord.target_board.in_([1, 2]),
                official_record_filter,
            )
        )
        replay_records = _latest_learning_batch_records(
            list(replay_result.scalars().all())
        )
        replay_record_count = len({
            (_safe_int(record.target_board), str(record.code or "").strip())
            for record in replay_records
            if str(record.code or "").strip()
        })

    first_target_incomplete = first_target_count < PROMOTION_MIN_FIRST_BOARD_SNAPSHOT_RECORDS
    second_target_incomplete = second_target_count < PROMOTION_MIN_SECOND_BOARD_SNAPSHOT_RECORDS
    raw_first_target_count = raw_counts.get((first_board_trade_date, 1), 0)
    raw_second_target_count = raw_counts.get((second_board_trade_date, 2), 0)
    physical_lane_incomplete = bool(
        raw_first_target_count < PROMOTION_MIN_FIRST_BOARD_SNAPSHOT_RECORDS
        or raw_second_target_count < PROMOTION_MIN_SECOND_BOARD_SNAPSHOT_RECORDS
    )
    missing_latest_trade_date = latest_trade_date_total <= 0
    missing_lane_snapshot = first_target_incomplete or second_target_incomplete
    stale_snapshot = bool(latest_record_date and latest_record_date < second_board_trade_date)
    version_mismatch = bool(version_mismatch_lanes)
    replay_missing = bool(replay_prediction_trade_date and replay_record_count <= 0)
    auction_health = await _build_auction_snapshot_health(db, first_board_trade_date)
    auction_snapshot_missing = bool(auction_health.get("missing"))
    auction_snapshot_stale = bool(auction_health.get("stale"))
    auction_field_degraded = bool(auction_health.get("field_degraded"))
    auction_snapshot_degraded = bool(auction_health.get("degraded"))

    details: list[str] = []
    if missing_latest_trade_date and not version_mismatch:
        details.append(
            f"{second_board_trade_date} 没有 promotion_prediction_record，不能展示旧预测当今天预测。"
        )
    if first_target_count <= 0:
        details.append(f"首板赛道 {first_board_trade_date} 缺少预测快照。")
    elif first_target_incomplete:
        details.append(
            f"首板赛道 {first_board_trade_date} 预测快照不完整，仅 {first_target_count} 条，"
            f"低于最低复盘口径 {PROMOTION_MIN_FIRST_BOARD_SNAPSHOT_RECORDS} 条。"
        )
    if second_target_count <= 0:
        details.append(f"二板赛道 {second_board_trade_date} 缺少预测快照。")
    elif second_target_incomplete:
        details.append(
            f"二板赛道 {second_board_trade_date} 预测快照不完整，仅 {second_target_count} 条，"
            f"低于最低复盘口径 {PROMOTION_MIN_SECOND_BOARD_SNAPSHOT_RECORDS} 条。"
        )
    if stale_snapshot and latest_record_date:
        details.append(f"最新预测记录停留在 {latest_record_date}，落后于最新涨停交易日 {second_board_trade_date}。")
    if version_mismatch:
        stale_versions = sorted({
            version
            for lane in version_mismatch_lanes
            for version in lane.get("versions", [])
        })
        details.append(
            f"当前模型为 {PROMOTION_MODEL_VERSION}，但对应赛道只有旧版本 "
            f"{', '.join(stale_versions)} 的正式快照；必须重新生成，不能沿用旧概率。"
        )
    if replay_missing and replay_prediction_trade_date:
        details.append(f"实际涨停回放需要的上一交易日 {replay_prediction_trade_date} 缺少预测记录。")
    if auction_snapshot_missing:
        details.append(f"{first_board_trade_date} 缺少 09:30 前 auction_data，竞价高开强攻路线本次没有真实竞价输入。")
    elif auction_snapshot_stale:
        details.append(
            f"{first_board_trade_date} 只有 {auction_health.get('timely_snapshot_count', 0)} / "
            f"{auction_health.get('latest_code_count', 0)} 只更新到 09:24 以后，"
            "早期快照不能冒充最终竞价。"
        )
    elif auction_field_degraded:
        details.append(
            f"{first_board_trade_date} 竞价快照只有 {auction_health.get('feed_complete_count', 0)} / "
            f"{auction_health.get('latest_code_count', 0)} 只具备价格、增量成交量、成交额和量比，"
            "价格快照不能冒充可执行竞价信号。"
        )

    snapshot_warn = bool(
        missing_latest_trade_date
        or missing_lane_snapshot
        or stale_snapshot
        or version_mismatch
        or replay_missing
    )
    should_warn = bool(snapshot_warn or auction_snapshot_missing or auction_snapshot_degraded)
    status = "ok"
    if physical_lane_incomplete or replay_missing:
        status = "missing_snapshot"
    elif version_mismatch:
        status = "stale_model_version"
    elif missing_latest_trade_date or missing_lane_snapshot:
        status = "missing_snapshot"
    elif stale_snapshot:
        status = "stale_snapshot"
    elif auction_snapshot_missing:
        status = "missing_auction_data"
    elif auction_snapshot_stale:
        status = "stale_auction_data"
    elif auction_field_degraded:
        status = "degraded_auction_data"

    return {
        "status": status,
        "should_warn": should_warn,
        "message": (
            "预测快照缺失"
            if status == "missing_snapshot"
            else "预测快照版本过期"
            if status == "stale_model_version"
            else "预测快照过期"
            if status == "stale_snapshot"
            else "竞价快照缺失"
            if auction_snapshot_missing
            else "竞价快照时点过早"
            if auction_snapshot_stale
            else "竞价字段不完整"
            if auction_field_degraded
            else "预测快照正常"
        ),
        "detail": "；".join(details) if details else "最新交易日已存在分赛道 prediction snapshot。",
        "expected_latest_trade_date": str(second_board_trade_date),
        "expected_trade_dates": {
            "target_1": str(first_board_trade_date),
            "target_2": str(second_board_trade_date),
        },
        "record_counts": {
            "latest_trade_date_total": latest_trade_date_total,
            "target_1": first_target_count,
            "target_2": second_target_count,
            "raw_target_1": raw_first_target_count,
            "raw_target_2": raw_second_target_count,
            "target_1_min_required": PROMOTION_MIN_FIRST_BOARD_SNAPSHOT_RECORDS,
            "target_2_min_required": PROMOTION_MIN_SECOND_BOARD_SNAPSHOT_RECORDS,
        },
        "snapshot_context_counts": {
            record_date: {
                target: dict(contexts)
                for target, contexts in targets.items()
            }
            for record_date, targets in context_counts.items()
        },
        "snapshot_context_unique_code_counts": {
            record_date: {
                target: {
                    context: len(codes)
                    for context, codes in contexts.items()
                }
                for target, contexts in targets.items()
            }
            for record_date, targets in context_unique_codes.items()
        },
        "snapshot_model_version_counts": {
            record_date: {
                target: dict(versions)
                for target, versions in targets.items()
            }
            for record_date, targets in model_version_counts.items()
        },
        "expected_model_version": PROMOTION_MODEL_VERSION,
        "model_version_mismatch": version_mismatch,
        "model_version_mismatch_lanes": version_mismatch_lanes,
        "canonical_close_context_priority": list(PROMOTION_CANONICAL_CLOSE_CONTEXTS),
        "intraday_confirmation_contexts": list(PROMOTION_INTRADAY_CONTEXTS),
        "auction_health": auction_health,
        "latest_record_trade_date": str(latest_record_date) if latest_record_date else None,
        "replay_expected_prediction_trade_date": str(replay_prediction_trade_date) if replay_prediction_trade_date else None,
        "replay_expected_record_count": replay_record_count,
        "replay_snapshot_missing": replay_missing,
        "checked_before_request_recording": True,
        "block_stale_display": snapshot_warn,
    }


def _build_market_ladder_context(limit_ups: list[dict]) -> dict:
    limit_up_count = len(limit_ups)
    if not limit_ups:
        return {
            "limit_up_count": 0,
            "first_board_count": 0,
            "second_board_count": 0,
            "board_height": 0,
            "high_board_count": 0,
            "sealed_ratio": 0.0,
            "broken_ratio": 0.0,
            "zero_break_seal_ratio": 0.0,
            "reopened_seal_ratio": 0.0,
            "avg_break_count": 0.0,
            "weak_seal_market": True,
            "weak_follow_through_market": True,
            "broad_first_board_overflow": False,
        }

    board_days = [_safe_int(item.get("consecutive_days"), 1) for item in limit_ups]
    break_counts = [_safe_int(item.get("break_count")) for item in limit_ups]
    board_height = max(board_days)
    first_board_count = sum(1 for days in board_days if days == 1)
    second_board_count = sum(1 for days in board_days if days == 2)
    high_board_count = sum(1 for days in board_days if days >= 3)
    sealed_count = sum(1 for break_count in break_counts if break_count == 0)
    broken_count = limit_up_count - sealed_count
    avg_break_count = sum(break_counts) / limit_up_count if limit_up_count else 0.0
    sealed_ratio = sealed_count / limit_up_count if limit_up_count else 0.0
    broken_ratio = broken_count / limit_up_count if limit_up_count else 0.0
    first_board_ratio = first_board_count / limit_up_count if limit_up_count else 0.0
    broad_first_board_overflow = (
        first_board_count >= 45
        and first_board_ratio >= 0.70
        and (sealed_ratio < 0.68 or avg_break_count >= 1.0)
    )
    weak_follow_through_market = (
        sealed_ratio < 0.58
        or broken_ratio >= 0.42
        or avg_break_count >= 1.30
        or broad_first_board_overflow
    )
    return {
        "limit_up_count": limit_up_count,
        "first_board_count": first_board_count,
        "second_board_count": second_board_count,
        "board_height": board_height,
        "high_board_count": high_board_count,
        "sealed_ratio": sealed_ratio,
        "broken_ratio": broken_ratio,
        # LimitUpPool 只保存最终封板股；break_count>0 表示开板后回封，
        # 不是“炸板未封”。保留旧字段兼容前端，同时给出不易误解的新名字。
        "zero_break_seal_ratio": sealed_ratio,
        "reopened_seal_ratio": broken_ratio,
        "avg_break_count": avg_break_count,
        "weak_seal_market": sealed_ratio < 0.6,
        "weak_follow_through_market": weak_follow_through_market,
        "broad_first_board_overflow": broad_first_board_overflow,
    }


async def _enrich_market_ladder_context(db: AsyncSession, trade_date: date, market_context: dict) -> dict:
    breadth = await _get_market_breadth_context(db, trade_date)
    market_up_ratio = _safe_float(breadth.get("up_ratio"))
    market_avg_change = _safe_float(breadth.get("avg_change"))
    breadth_context = {
        "market_breadth": breadth,
        "market_up_ratio": round(market_up_ratio, 4),
        "market_avg_change": round(market_avg_change, 3),
        "broad_rising_market": bool(market_up_ratio >= 0.70 and market_avg_change >= 0.8),
    }
    result = await db.execute(
        select(
            MarketSentiment.sentiment_cycle,
            MarketSentiment.limit_down_count,
            MarketSentiment.broken_limit_count,
            MarketSentiment.seal_rate,
            MarketSentiment.board_height,
            MarketSentiment.main_net_inflow,
        )
        .where(MarketSentiment.trade_date == trade_date)
        .limit(1)
    )
    row = result.one_or_none()
    if row is None:
        return {
            **market_context,
            **breadth_context,
            "sentiment_cycle": "recovery",
            "market_risk_level": "weak" if market_context.get("weak_follow_through_market") else "normal",
            "washout_reversal_setup": bool(market_up_ratio <= 0.38 and market_avg_change <= -0.5),
        }

    sentiment_cycle = _normalize_promotion_sentiment(row[0])
    limit_down_count = _safe_int(row[1])
    broken_limit_count = _safe_int(row[2])
    seal_rate_pct = _safe_float(row[3])
    board_height = _safe_int(row[4], _safe_int(market_context.get("board_height")))
    main_net_inflow = _safe_float(row[5])
    weak_liquidity = main_net_inflow <= -300 or limit_down_count >= 15
    washout_reversal_setup = bool(
        market_up_ratio <= 0.38
        and market_avg_change <= -0.5
        and (limit_down_count >= 15 or main_net_inflow <= -500)
    )
    hostile_market = (
        sentiment_cycle in {"divergence", "freezing"}
        and (
            seal_rate_pct < 58.0
            or broken_limit_count >= 60
            or main_net_inflow <= -500
            or bool(market_context.get("weak_follow_through_market"))
        )
    )
    market_risk_level = "hostile" if hostile_market else "weak" if weak_liquidity or market_context.get("weak_follow_through_market") else "normal"
    return {
        **market_context,
        **breadth_context,
        "sentiment_cycle": sentiment_cycle,
        "limit_down_count": limit_down_count,
        "market_broken_limit_count": broken_limit_count,
        "sentiment_seal_rate": seal_rate_pct,
        "sentiment_board_height": board_height,
        "main_net_inflow": main_net_inflow,
        "weak_liquidity_market": weak_liquidity,
        "market_risk_level": market_risk_level,
        "hostile_follow_through_market": hostile_market,
        # 只描述“次日反弹形态的候选环境”，不据此放开任何交易闸门。
        "washout_reversal_setup": washout_reversal_setup,
    }


def _market_regime_penalty(market_context: dict, *, target_board: int, route: str = "") -> float:
    risk_level = str(market_context.get("market_risk_level") or "")
    sentiment = str(market_context.get("sentiment_cycle") or "")
    main_net_inflow = _safe_float(market_context.get("main_net_inflow"))
    penalty = 0.0

    if risk_level == "hostile":
        penalty += 0.14 if target_board == 2 else 10.0
    elif risk_level == "weak":
        penalty += 0.08 if target_board == 2 else 6.0

    if sentiment == "freezing":
        penalty += 0.06 if target_board == 2 else 4.0
    elif sentiment == "divergence":
        penalty += 0.035 if target_board == 2 else 3.0

    if main_net_inflow <= -700:
        penalty += 0.055 if target_board == 2 else 4.0
    elif main_net_inflow <= -300:
        penalty += 0.035 if target_board == 2 else 2.5

    if bool(market_context.get("broad_first_board_overflow")):
        penalty += 0.035 if target_board == 2 else 2.0

    if target_board == 1 and route in {"fresh_relay_start", "hot_primary", "relay_fillup"} and risk_level != "normal":
        penalty += 2.5
    if target_board == 1 and route == "oversold_reversal_start" and risk_level == "hostile":
        penalty = max(0.0, penalty - 5.0)
    if target_board == 1 and route == "pre_board_probe_start" and risk_level == "hostile":
        penalty = max(0.0, penalty - 3.0)
    return penalty


def _calc_sector_continuity_score(sector: dict) -> float:
    strength = min(max(_safe_float(sector.get("strength_score")), 0.0), 100.0) / 100
    consecutive_days = _safe_int(sector.get("consecutive_days"))
    limit_up_count = _safe_int(sector.get("limit_up_count"))
    fund_flow = _safe_float(sector.get("fund_flow"))
    change_pct = _safe_float(sector.get("change_pct"))

    score = strength * 0.45
    if consecutive_days >= 3:
        score += 0.28
    elif consecutive_days == 2:
        score += 0.18
    elif consecutive_days == 1:
        score += 0.1

    if limit_up_count >= 5:
        score += 0.16
    elif limit_up_count >= 3:
        score += 0.1
    elif limit_up_count >= 1:
        score += 0.04

    if fund_flow > 5:
        score += 0.08
    elif fund_flow > 0:
        score += 0.04
    elif fund_flow < -2:
        score -= 0.08

    if change_pct > 2:
        score += 0.04
    elif change_pct < -1:
        score -= 0.06

    return max(0.0, min(score, 1.0))


def _is_broad_rotation_cluster_sector(sector: dict | None, market_context: dict | None = None) -> bool:
    sector = sector or {}
    market_context = market_context or {}
    if not bool(market_context.get("broad_first_board_overflow")):
        return False
    if bool(sector.get("sector_crowded_stale_theme")):
        return False
    limit_up_count = _safe_int(sector.get("limit_up_count"))
    if limit_up_count < BROAD_ROTATION_MAINLINE_MIN_LIMIT_UP_COUNT:
        return False
    if _safe_float(sector.get("change_pct")) < -3.0:
        return False
    return bool(
        limit_up_count >= BROAD_ROTATION_MAINLINE_STRONG_LIMIT_UP_COUNT
        or _safe_float(sector.get("sector_limit_up_delta")) >= 1.5
        or _safe_float(sector.get("sector_rotation_score")) >= BROAD_ROTATION_MAINLINE_MIN_ROTATION_SCORE
        or _safe_float(sector.get("sector_strength_delta")) >= 5.0
    )


def _parse_limit_up_minutes(value: str | None) -> int | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    if ":" in raw:
        parts = raw.split(":")
        if len(parts) < 2:
            return None
        hour_text, minute_text = parts[0], parts[1]
    else:
        digits = "".join(ch for ch in raw if ch.isdigit())
        if len(digits) >= 6:
            hour_text, minute_text = digits[:2], digits[2:4]
        elif len(digits) == 4:
            hour_text, minute_text = digits[:2], digits[2:4]
        elif len(digits) == 3:
            hour_text, minute_text = digits[:1], digits[1:3]
        else:
            return None
    try:
        hours = int(hour_text)
        minutes = int(minute_text)
    except ValueError:
        return None
    if not (0 <= hours <= 23 and 0 <= minutes <= 59):
        return None
    return hours * 60 + minutes


def _high_board_distribution_penalty(
    market_context: dict,
    *,
    sector_continuity: float,
    has_high_board_risk: bool,
    turnover: float,
    break_count: int,
    limit_up_minutes: int | None = None,
) -> float:
    board_height = _safe_int(market_context.get("board_height"))
    penalty = 0.0

    if board_height >= 5:
        penalty += 0.05
    elif board_height >= 4:
        penalty += 0.03

    if board_height >= 4 and sector_continuity < 0.45:
        penalty += 0.04
    if has_high_board_risk:
        penalty += 0.06
    if turnover >= 16:
        penalty += 0.04
    if break_count >= 1:
        penalty += 0.05
    if limit_up_minutes is not None and limit_up_minutes >= 14 * 60:
        penalty += 0.03
    if bool(market_context.get("weak_seal_market")):
        penalty += 0.02
    if bool(market_context.get("weak_follow_through_market")):
        penalty += 0.04
    if bool(market_context.get("broad_first_board_overflow")):
        penalty += 0.05
    avg_break_count = _safe_float(market_context.get("avg_break_count"))
    if avg_break_count >= 2.0:
        penalty += 0.035
    elif avg_break_count >= 1.2:
        penalty += 0.02
    if _safe_float(market_context.get("broken_ratio")) >= 0.42:
        penalty += 0.025

    return min(penalty, 0.32)


def _calc_doji_signal(bar: dict) -> bool:
    open_price = _safe_float(bar.get("open"))
    close_price = _safe_float(bar.get("close"))
    high_price = _safe_float(bar.get("high"))
    low_price = _safe_float(bar.get("low"))
    if open_price <= 0 or close_price <= 0 or high_price <= 0 or low_price <= 0 or high_price <= low_price:
        return False
    body_pct = abs(close_price - open_price) / open_price * 100
    range_pct = (high_price - low_price) / low_price * 100
    return body_pct <= 1.0 and range_pct >= 1.4


def _calc_failed_reversal_signal(bar: dict) -> bool:
    open_price = _safe_float(bar.get("open"))
    close_price = _safe_float(bar.get("close"))
    high_price = _safe_float(bar.get("high"))
    low_price = _safe_float(bar.get("low"))
    if open_price <= 0 or close_price <= 0 or high_price <= 0 or low_price <= 0 or high_price <= low_price:
        return False

    candle_range = high_price - low_price
    upper_shadow_pct = max(high_price - max(open_price, close_price), 0.0) / open_price * 100
    close_position = (close_price - low_price) / candle_range if candle_range > 0 else 0.5
    return upper_shadow_pct >= 2.2 and close_position <= 0.38


def _calc_lower_shadow_ratio(bar: dict) -> float:
    open_price = _safe_float(bar.get("open"))
    close_price = _safe_float(bar.get("close"))
    high_price = _safe_float(bar.get("high"))
    low_price = _safe_float(bar.get("low"))
    if open_price <= 0 or close_price <= 0 or high_price <= 0 or low_price <= 0 or high_price <= low_price:
        return 0.0
    candle_range = high_price - low_price
    if candle_range <= 0:
        return 0.0
    lower_shadow = max(min(open_price, close_price) - low_price, 0.0)
    return lower_shadow / candle_range


def _estimate_main_inflow_pct(main_net_inflow: float, amount: float) -> float:
    if amount <= 0:
        return 0.0
    return round(main_net_inflow / amount * 100.0, 2)


def _calc_bar_amplitude_pct(bar: dict) -> float:
    high_price = _safe_float(bar.get("high"))
    low_price = _safe_float(bar.get("low"))
    if high_price <= 0 or low_price <= 0 or high_price <= low_price:
        return 0.0
    return (high_price / low_price - 1.0) * 100.0


def _mean(values: list[float]) -> float:
    cleaned = [value for value in values if value is not None]
    if not cleaned:
        return 0.0
    return sum(cleaned) / len(cleaned)


def _calc_close_strength(detail: dict) -> float:
    high_price = _safe_float(detail.get("high"))
    low_price = _safe_float(detail.get("low"))
    close_price = _safe_float(detail.get("price") or detail.get("close"))
    if high_price <= low_price or close_price <= 0:
        return 0.5
    return max(0.0, min((close_price - low_price) / (high_price - low_price), 1.0))


def _default_burst_pullback_restart_meta() -> dict:
    return {
        "burst_pullback_restart_ready": False,
        "burst_pullback_score": 0.0,
        "burst_pullback_label": "",
        "burst_date": "",
        "burst_volume_ratio": 0.0,
        "burst_pullback_depth_pct": 0.0,
        "burst_shrink_ratio": 1.0,
        "burst_rebound_pct": 0.0,
        "burst_high_gap_pct": 999.0,
        "burst_restart_days": 0,
        "burst_restart_volume_ratio": 0.0,
    }


def _build_burst_pullback_restart_meta(bars: list[dict]) -> dict:
    """识别爆量点火后缩量深调、再度收复均线的主升浪前兆。"""
    default_meta = _default_burst_pullback_restart_meta()
    cleaned_bars = [
        item
        for item in (bars or [])
        if _safe_float(item.get("close")) > 0
        and _safe_float(item.get("high")) > 0
        and _safe_float(item.get("low")) > 0
        and _safe_float(item.get("volume")) > 0
    ]
    if len(cleaned_bars) < 18:
        return default_meta

    latest_bar = cleaned_bars[-1]
    latest_close = _safe_float(latest_bar.get("close"))
    latest_volume = _safe_float(latest_bar.get("volume"))
    closes = [_safe_float(item.get("close")) for item in cleaned_bars]
    highs = [_safe_float(item.get("high")) for item in cleaned_bars]
    volumes = [_safe_float(item.get("volume")) for item in cleaned_bars]
    ma10 = _mean(closes[-10:]) if len(closes) >= 10 else 0.0
    ma20 = _mean(closes[-20:]) if len(closes) >= 20 else 0.0
    latest_prev_close = _safe_float(latest_bar.get("prev_close"))
    if latest_prev_close <= 0 and len(cleaned_bars) >= 2:
        latest_prev_close = _safe_float(cleaned_bars[-2].get("close"))
    latest_change_pct = _safe_float(latest_bar.get("change_pct"))
    if latest_change_pct == 0 and latest_prev_close > 0:
        latest_change_pct = _safe_percent_change(latest_close, latest_prev_close)

    best_meta = default_meta
    search_start = max(10, len(cleaned_bars) - 55)
    search_end = max(search_start, len(cleaned_bars) - 5)
    for idx in range(search_start, search_end):
        burst_bar = cleaned_bars[idx]
        burst_volume = _safe_float(burst_bar.get("volume"))
        if burst_volume <= 0:
            continue
        prev_volumes = volumes[max(0, idx - 20):idx]
        prev10_avg = _mean(prev_volumes[-10:])
        prev20_avg = _mean(prev_volumes)
        if prev10_avg <= 0 and prev20_avg <= 0:
            continue
        burst_volume_ratio = max(
            burst_volume / prev10_avg if prev10_avg > 0 else 0.0,
            burst_volume / prev20_avg if prev20_avg > 0 else 0.0,
        )
        burst_prev_close = _safe_float(burst_bar.get("prev_close"))
        if burst_prev_close <= 0 and idx > 0:
            burst_prev_close = _safe_float(cleaned_bars[idx - 1].get("close"))
        burst_high = _safe_float(burst_bar.get("high"))
        burst_close = _safe_float(burst_bar.get("close"))
        burst_change_pct = _safe_float(burst_bar.get("change_pct"))
        if burst_change_pct == 0 and burst_prev_close > 0:
            burst_change_pct = _safe_percent_change(burst_close, burst_prev_close)
        burst_high_pct = _safe_percent_change(burst_high, burst_prev_close) if burst_prev_close > 0 else 0.0
        if burst_volume_ratio < 3.0 or max(burst_change_pct, burst_high_pct) < 5.0:
            continue

        after_burst = cleaned_bars[idx + 1:]
        if len(after_burst) < 4:
            continue
        low_rel_idx, low_bar = min(enumerate(after_burst), key=lambda item: _safe_float(item[1].get("low")) or 999999.0)
        low_idx = idx + 1 + low_rel_idx
        low_price = _safe_float(low_bar.get("low"))
        if low_price <= 0:
            continue
        pullback_depth_pct = _safe_percent_change(burst_high, low_price)
        days_since_low = len(cleaned_bars) - low_idx - 1
        if days_since_low < 1:
            continue
        low_window = cleaned_bars[max(idx + 1, low_idx - 1): min(len(cleaned_bars), low_idx + 3)]
        low_window_volume = _mean([_safe_float(item.get("volume")) for item in low_window if _safe_float(item.get("volume")) > 0])
        burst_shrink_ratio = low_window_volume / burst_volume if burst_volume > 0 and low_window_volume > 0 else 1.0
        rebound_pct = _safe_percent_change(latest_close, low_price)
        burst_high_gap_pct = max(_safe_percent_change(burst_high, latest_close), 0.0)
        previous_volume = _mean(volumes[-11:-1]) if len(volumes) >= 11 else _mean(volumes[:-1])
        restart_volume_ratio = latest_volume / previous_volume if previous_volume > 0 else 0.0
        reclaim_ma = bool(
            (ma10 > 0 and latest_close >= ma10 * 0.99)
            or (ma20 > 0 and latest_close >= ma20 * 0.97)
            or latest_close >= max(closes[max(0, len(closes) - 5):]) * 0.985
        )
        score = _clamp_score(
            46.0
            + _range_score(burst_volume_ratio, ideal_low=3.2, ideal_high=7.5, min_value=2.0, max_value=13.0) * 0.16
            + _range_score(pullback_depth_pct, ideal_low=12.0, ideal_high=28.0, min_value=8.0, max_value=38.0) * 0.22
            + _inverse_score(burst_shrink_ratio, good=0.30, bad=0.66) * 0.20
            + _range_score(rebound_pct, ideal_low=6.0, ideal_high=22.0, min_value=2.0, max_value=36.0) * 0.16
            + _inverse_score(burst_high_gap_pct, good=2.8, bad=22.0) * 0.12
            + _range_score(float(days_since_low), ideal_low=2.0, ideal_high=16.0, min_value=1.0, max_value=30.0) * 0.08
            + _range_score(restart_volume_ratio, ideal_low=0.75, ideal_high=3.2, min_value=0.25, max_value=5.8) * 0.06
        )
        ready = bool(
            score >= 62.0
            and 10.0 <= pullback_depth_pct <= 34.0
            and burst_shrink_ratio <= 0.58
            and rebound_pct >= 6.0
            and days_since_low <= 25
            and burst_high_gap_pct <= 24.0
            and latest_change_pct <= 9.2
            and reclaim_ma
        )
        if score <= _safe_float(best_meta.get("burst_pullback_score")):
            continue
        trade_day = burst_bar.get("trade_date")
        best_meta = {
            "burst_pullback_restart_ready": ready,
            "burst_pullback_score": round(score, 2),
            "burst_pullback_label": "爆量深调再启动" if ready else "爆量深调观察",
            "burst_date": trade_day.isoformat() if hasattr(trade_day, "isoformat") else str(trade_day or ""),
            "burst_volume_ratio": round(burst_volume_ratio, 2),
            "burst_pullback_depth_pct": round(pullback_depth_pct, 2),
            "burst_shrink_ratio": round(burst_shrink_ratio, 3),
            "burst_rebound_pct": round(rebound_pct, 2),
            "burst_high_gap_pct": round(burst_high_gap_pct, 2),
            "burst_restart_days": days_since_low,
            "burst_restart_volume_ratio": round(restart_volume_ratio, 3),
        }
    return best_meta


def _build_first_board_limit_probe_shape(detail: dict, kline_context: dict | None = None) -> dict:
    detail = detail or {}
    kline_context = kline_context or {}
    intraday_high_pct = _safe_float(detail.get("intraday_high_pct"))
    upper_gap_pct = _safe_float(detail.get("upper_gap_pct"))
    lower_gap_pct = _safe_float(detail.get("lower_gap_pct"))
    close_position = _safe_float(detail.get("close_position"))
    change_pct = _safe_float(detail.get("change_pct"))
    volume_ratio = _safe_float(detail.get("volume_ratio"))
    main_inflow_pct = _safe_float(detail.get("main_net_inflow_pct"))
    limit_probe = bool(detail.get("limit_probe"))
    preheat_breakout = bool(detail.get("preheat_breakout"))
    failed_reversal_count = _safe_int(kline_context.get("failed_reversal_count"))
    burst_pullback_ready = bool(
        detail.get("burst_pullback_restart_ready")
        or kline_context.get("burst_pullback_restart_ready")
    )
    burst_pullback_score = max(
        _safe_float(detail.get("burst_pullback_score")),
        _safe_float(kline_context.get("burst_pullback_score")),
    )
    touch_board_pullback = bool(detail.get("touch_board_pullback")) or bool(
        limit_probe
        and intraday_high_pct >= 8.0
        and (upper_gap_pct >= 3.2 or close_position <= 0.48)
        and close_position >= 0.20
        and change_pct > -5.5
        and volume_ratio <= 7.2
    )
    trend_preheat = bool(detail.get("trend_preheat")) or bool(
        kline_context.get("near_trend_high")
        and kline_context.get("ma_alignment")
        and (
            kline_context.get("trend_volume_release")
            or (1.05 <= volume_ratio <= 3.6 and close_position >= 0.55)
        )
    )
    panic_repair = bool(
        (detail.get("panic_flush") or detail.get("washout_reversal"))
        and -6.5 <= change_pct <= -1.2
        and lower_gap_pct >= 2.0
        and close_position >= 0.50
        and volume_ratio >= 0.9
    )

    bonus_points = 0.0
    penalty_points = 0.0
    tags: list[str] = []
    supportive = False
    failed_probe = False
    precursor_type = "neutral"

    if (
        limit_probe
        and upper_gap_pct <= 1.6
        and close_position >= 0.65
        and 1.0 <= volume_ratio <= 4.8
        and main_inflow_pct >= 3.0
    ):
        bonus_points += 3.5
        tags.append("临板强承接")
        supportive = True
        precursor_type = "hard_support_probe"
    elif preheat_breakout and close_position >= 0.62 and 1.1 <= volume_ratio <= 3.8:
        bonus_points += 2.0
        tags.append("放量预热")
        supportive = True
        precursor_type = "volume_preheat"

    if touch_board_pullback:
        bonus_points += 4.0
        tags.append("摸板回落蓄势")
        supportive = True
        precursor_type = "touch_board_pullback"
    if burst_pullback_ready:
        bonus_points += 3.5 if burst_pullback_score >= 68.0 else 2.6
        tags.append("爆量深调再起")
        supportive = True
        precursor_type = "burst_pullback_restart"
    if trend_preheat:
        bonus_points += 2.0
        tags.append("短均主升预热")
        supportive = True
        if precursor_type == "neutral":
            precursor_type = "short_ma_trend_preheat"
    if panic_repair:
        bonus_points += 2.4
        tags.append("低位恐慌修复")
        supportive = True
        if precursor_type == "neutral":
            precursor_type = "panic_repair"

    weak_cashout_probe = bool(
        limit_probe
        and (
            close_position <= 0.18
            or change_pct <= -5.0
            or (volume_ratio >= 7.5 and main_inflow_pct < 2.0)
        )
    )
    if weak_cashout_probe:
        penalty_points += 4.5
        tags.append("冲高兑现")
        failed_probe = True
        precursor_type = "failed_probe_cashout"
    elif (
        intraday_high_pct >= 6.0
        and close_position <= 0.34
        and not touch_board_pullback
        and not burst_pullback_ready
    ):
        penalty_points += 2.5
        tags.append("冲高承接弱")
        failed_probe = True
        if precursor_type == "neutral":
            precursor_type = "weak_intraday_reversal"

    if (
        volume_ratio >= 6.8
        and main_inflow_pct < 2.0
        and not bool(detail.get("washout_reversal"))
        and not burst_pullback_ready
        and not touch_board_pullback
    ):
        penalty_points += 2.0
        tags.append("放量不聚焦")
    if failed_reversal_count > 0 and close_position < 0.55:
        penalty_points += min(2.5, failed_reversal_count * 1.5)
        failed_probe = True
    if lower_gap_pct >= 8.0 and change_pct <= 2.0:
        penalty_points += 1.5
        tags.append("下影修复不足")

    label = " / ".join(tags[:3]) if tags else "形态中性"
    return {
        "limit_probe_shape_label": label,
        "limit_probe_shape_bonus_points": round(bonus_points, 2),
        "limit_probe_shape_penalty_points": round(penalty_points, 2),
        "limit_probe_shape_score": _clamp_score(50.0 + bonus_points * 5.0 - penalty_points * 5.0),
        "limit_probe_shape_supportive": supportive,
        "limit_probe_shape_failed": failed_probe,
        "touch_board_pullback": touch_board_pullback,
        "trend_preheat": trend_preheat,
        "panic_repair": panic_repair,
        "pre_board_precursor_type": precursor_type,
        "pre_board_precursor_label": label,
    }


def _weekday_gap(start_date: date, end_date: date) -> int:
    if end_date <= start_date:
        return 0
    days = 0
    current = start_date + timedelta(days=1)
    while current <= end_date:
        if current.weekday() < 5:
            days += 1
        current += timedelta(days=1)
    return days


def _evaluate_platform_cycle_window(window_bars: list[dict], latest_close: float, config: dict) -> dict | None:
    if len(window_bars) < 8 or latest_close <= 0:
        return None

    highs = [_safe_float(item.get("high")) for item in window_bars if _safe_float(item.get("high")) > 0]
    lows = [_safe_float(item.get("low")) for item in window_bars if _safe_float(item.get("low")) > 0]
    volumes = [_safe_float(item.get("volume")) for item in window_bars if _safe_float(item.get("volume")) >= 0]
    if not highs or not lows or len(volumes) < len(window_bars):
        return None

    recent_three = window_bars[-3:]
    earlier_window = window_bars[:-3] or window_bars
    reference_window = window_bars[:-3] or window_bars[:-1] or window_bars

    platform_high = max(highs)
    platform_low = min(lows)
    platform_range_pct = _safe_percent_change(platform_high, platform_low)
    reference_highs = [_safe_float(item.get("high")) for item in reference_window if _safe_float(item.get("high")) > 0]
    reference_high = max(reference_highs) if reference_highs else platform_high
    overhead_gap_pct = max(_safe_percent_change(reference_high, latest_close), 0.0) if reference_high > 0 else 0.0

    recent_three_volumes = [_safe_float(item.get("volume")) for item in recent_three]
    earlier_volumes = [_safe_float(item.get("volume")) for item in earlier_window]
    platform_avg_volume = _mean(volumes)
    earlier_avg_volume = _mean(earlier_volumes) or platform_avg_volume
    recent_three_avg_volume = _mean(recent_three_volumes)
    volume_shrink_ratio = recent_three_avg_volume / earlier_avg_volume if earlier_avg_volume > 0 else 1.0
    volume_suffocation_ratio = recent_three_avg_volume / platform_avg_volume if platform_avg_volume > 0 else 1.0
    recent_volume_ceiling_ratio = (
        max(recent_three_volumes) / platform_avg_volume if platform_avg_volume > 0 and recent_three_volumes else 1.0
    )
    recent_amplitude_pct = _mean([_calc_bar_amplitude_pct(item) for item in recent_three])

    has_platform_contraction = platform_range_pct <= _safe_float(config.get("max_range_pct"))
    has_volume_contraction = volume_shrink_ratio <= _safe_float(config.get("max_shrink_ratio"))
    has_volume_suffocation = (
        volume_suffocation_ratio <= _safe_float(config.get("max_suffocation_ratio"))
        and recent_volume_ceiling_ratio <= _safe_float(config.get("max_recent_volume_ceiling_ratio"))
        and recent_amplitude_pct <= _safe_float(config.get("max_recent_amplitude_pct"))
    )
    near_breakout = overhead_gap_pct <= _safe_float(config.get("max_overhead_gap_pct"))

    range_fit = 1.0 - min(platform_range_pct / max(_safe_float(config.get("max_range_pct")), 0.1), 1.6)
    suffocation_fit = 1.0 - min(volume_suffocation_ratio / max(_safe_float(config.get("max_suffocation_ratio")), 0.1), 1.6)
    overhead_fit = 1.0 - min(overhead_gap_pct / max(_safe_float(config.get("max_overhead_gap_pct")), 0.1), 1.6)
    recent_amp_fit = 1.0 - min(recent_amplitude_pct / max(_safe_float(config.get("max_recent_amplitude_pct")), 0.1), 1.6)
    duration_fit = len(window_bars) / max(_safe_int(config.get("max_days"), len(window_bars)), 1)

    fit_score = round(
        max(range_fit, -1.0) * 0.28
        + max(suffocation_fit, -1.0) * 0.34
        + max(overhead_fit, -1.0) * 0.18
        + max(recent_amp_fit, -1.0) * 0.1
        + duration_fit * 0.1,
        3,
    )

    qualifies = has_platform_contraction and has_volume_suffocation and near_breakout
    return {
        "platform_cycle_type": str(config.get("type") or ""),
        "platform_cycle_label": str(config.get("label") or ""),
        "platform_cycle_days": len(window_bars),
        "platform_range_pct": round(platform_range_pct, 2),
        "volume_shrink_ratio": round(volume_shrink_ratio, 3),
        "volume_suffocation_ratio": round(volume_suffocation_ratio, 3),
        "recent_volume_ceiling_ratio": round(recent_volume_ceiling_ratio, 3),
        "recent_amplitude_pct": round(recent_amplitude_pct, 2),
        "platform_avg_volume": round(platform_avg_volume, 2),
        "platform_reference_high": round(reference_high, 2),
        "overhead_gap_pct": round(overhead_gap_pct, 2),
        "has_platform_contraction": has_platform_contraction,
        "has_volume_contraction": has_volume_contraction,
        "has_volume_suffocation": has_volume_suffocation,
        "near_breakout": near_breakout,
        "fit_score": fit_score,
        "qualifies": qualifies,
    }


def _resolve_platform_cycle_context(completed_bars: list[dict], latest_close: float) -> dict:
    best_fallback: dict | None = None

    for config in PLATFORM_CYCLE_CONFIGS:
        min_days = _safe_int(config.get("min_days"))
        max_days = min(_safe_int(config.get("max_days")), len(completed_bars))
        if max_days < min_days:
            continue

        for days in range(max_days, min_days - 1, -1):
            metrics = _evaluate_platform_cycle_window(completed_bars[-days:], latest_close, config)
            if not metrics:
                continue
            if metrics.get("qualifies"):
                return metrics
            if best_fallback is None or _safe_float(metrics.get("fit_score")) > _safe_float(best_fallback.get("fit_score")):
                best_fallback = metrics

    if best_fallback is not None:
        return best_fallback

    fallback_config = PLATFORM_CYCLE_CONFIGS[-1]
    fallback_days = min(len(completed_bars), _safe_int(fallback_config.get("min_days"), 8))
    metrics = _evaluate_platform_cycle_window(completed_bars[-fallback_days:], latest_close, fallback_config)
    if metrics is not None:
        return metrics
    return {
        "platform_cycle_type": "short",
        "platform_cycle_label": "短平台",
        "platform_cycle_days": fallback_days,
        "platform_range_pct": 0.0,
        "volume_shrink_ratio": 1.0,
        "volume_suffocation_ratio": 1.0,
        "recent_volume_ceiling_ratio": 1.0,
        "recent_amplitude_pct": 0.0,
        "platform_avg_volume": 0.0,
        "platform_reference_high": 0.0,
        "overhead_gap_pct": 0.0,
        "has_platform_contraction": False,
        "has_volume_contraction": False,
        "has_volume_suffocation": False,
        "near_breakout": False,
        "fit_score": 0.0,
        "qualifies": False,
    }


def _summarize_kline_confirmation(context: dict) -> str:
    tags: list[str] = []
    cycle_label = str(context.get("platform_cycle_label") or "")
    if bool(context.get("qualified_doji_confirmation")):
        tags.append("十字星蓄势")
    elif bool(context.get("has_doji_confirmation")):
        tags.append("十字星待确认")
    if bool(context.get("has_platform_contraction")):
        tags.append(f"{cycle_label}压缩" if cycle_label else "平台压缩")
    if bool(context.get("has_volume_suffocation")):
        tags.append(f"{cycle_label}量窒息" if cycle_label else "量窒息")
    elif bool(context.get("has_volume_contraction")):
        tags.append("缩量蓄势")
    if bool(context.get("near_breakout")):
        tags.append("贴近前高")
    if bool(context.get("trend_acceleration_ready")):
        tags.append("趋势新高加速")
    elif bool(context.get("trend_breakout")) and bool(context.get("ma_alignment")):
        tags.append("趋势突破预备")
    elif bool(context.get("near_trend_high")) and bool(context.get("ma_alignment")):
        tags.append("贴近趋势前高")
    if bool(context.get("burst_pullback_restart_ready")):
        tags.append("爆量深调再起")
    if bool(context.get("launch_profile_ready")):
        tags.append("中低位修复活跃")
    if _safe_int(context.get("failed_reversal_count")) > 0:
        tags.append("上冲回落风险")
    if not tags:
        return "K线形态中性"
    return " / ".join(tags[:4])


def _build_first_board_kline_confirmation(bars: list[dict]) -> dict:
    default_context = {
        "confirmation_bonus": 0.0,
        "confirmation_penalty": 0.02,
        "confirmation_score": 0.0,
        "has_doji_confirmation": False,
        "qualified_doji_confirmation": False,
        "has_platform_contraction": False,
        "has_volume_contraction": False,
        "has_volume_suffocation": False,
        "near_breakout": False,
        "failed_reversal_count": 0,
        "platform_cycle_type": "short",
        "platform_cycle_label": "短平台",
        "platform_cycle_days": 0,
        "platform_range_pct": 0.0,
        "volume_shrink_ratio": 1.0,
        "volume_suffocation_ratio": 1.0,
        "recent_volume_ceiling_ratio": 1.0,
        "recent_amplitude_pct": 0.0,
        "latest_close": 0.0,
        "platform_reference_high": 0.0,
        "overhead_gap_pct": 0.0,
        "support_zone_price": 0.0,
        "support_zone_gap_pct": 999.0,
        "support_hold_days": 0,
        "support_rebound_count": 0,
        "lower_shadow_absorption": 0.0,
        "has_support_absorption": False,
        "near_trend_high": False,
        "trend_breakout": False,
        "trend_acceleration_ready": False,
        "ma_alignment": False,
        "trend_volume_release": False,
        "trend_breakout_gap_pct": 0.0,
        "platform_confirmation_count": 0,
        "trend_confirmation_count": 0,
        "ma5": 0.0,
        "ma10": 0.0,
        "ma20": 0.0,
        "strict_confirmation_count": 0,
        "strict_ready": False,
        "provisional_last_bar": False,
        "insufficient_history": True,
        "long_cycle_profile": {
            "long_cycle_regime": "neutral",
            "long_cycle_regime_label": "长周期结构中性",
            "quality_setup_score": 0.0,
            "shape_ready": False,
            "direct_buy_ready": False,
            "requires_intraday_confirmation": True,
            "setup_phase": "insufficient_history",
            "setup_phase_label": "历史不足",
        },
        **_default_burst_pullback_restart_meta(),
        **build_stock_launch_profile([]),
        "summary": "历史K线不足，按中性偏谨慎处理",
    }
    if len(bars) < 8:
        return default_context

    latest_bar = bars[-1]
    latest_is_provisional = bool(latest_bar.get("is_provisional"))
    completed_bars = bars[:-1] if latest_is_provisional else bars
    minimum_completed_bars = 7 if latest_is_provisional else 8
    if len(completed_bars) < minimum_completed_bars:
        context = {**default_context, "provisional_last_bar": latest_is_provisional}
        if latest_is_provisional:
            context["confirmation_penalty"] = 0.05
            context["summary"] = "盘中K线未收盘，历史K线不足，按谨慎处理"
        return context

    launch_profile = build_stock_launch_profile(completed_bars)
    long_cycle_profile = build_long_cycle_profile(
        closes=[_safe_float(item.get("close")) for item in completed_bars],
        highs=[_safe_float(item.get("high")) for item in completed_bars],
        lows=[_safe_float(item.get("low")) for item in completed_bars],
        volumes=[_safe_float(item.get("volume")) for item in completed_bars],
    )

    recent = completed_bars[-5:]
    recent_three = completed_bars[-3:]
    support_window = completed_bars[-5:]
    latest_close = _safe_float(completed_bars[-1].get("close"))
    platform_context = _resolve_platform_cycle_context(completed_bars, latest_close)
    burst_pullback_meta = _build_burst_pullback_restart_meta(completed_bars)
    burst_pullback_ready = bool(burst_pullback_meta.get("burst_pullback_restart_ready"))
    platform_range_pct = _safe_float(platform_context.get("platform_range_pct"))
    volume_shrink_ratio = _safe_float(platform_context.get("volume_shrink_ratio"), 1.0)
    volume_suffocation_ratio = _safe_float(platform_context.get("volume_suffocation_ratio"), 1.0)
    recent_volume_ceiling_ratio = _safe_float(platform_context.get("recent_volume_ceiling_ratio"), 1.0)
    recent_amplitude_pct = _safe_float(platform_context.get("recent_amplitude_pct"), 0.0)
    overhead_gap_pct = _safe_float(platform_context.get("overhead_gap_pct"))
    recent_doji_count = sum(1 for item in recent_three if _calc_doji_signal(item))
    failed_reversal_count = sum(1 for item in recent if _calc_failed_reversal_signal(item))
    support_lows = [_safe_float(item.get("low")) for item in support_window if _safe_float(item.get("low")) > 0]
    support_zone_price = min(support_lows) if support_lows else 0.0
    support_zone_gap_pct = _safe_percent_change(latest_close, support_zone_price) if support_zone_price > 0 else 999.0
    support_hold_days = 0
    support_rebound_count = 0
    lower_shadow_absorption = 0.0
    if support_zone_price > 0:
        lower_shadow_absorption = _mean([_calc_lower_shadow_ratio(item) for item in support_window])
        for item in support_window:
            low_price = _safe_float(item.get("low"))
            close_price = _safe_float(item.get("close"))
            open_price = _safe_float(item.get("open"))
            high_price = _safe_float(item.get("high"))
            if low_price <= 0 or close_price <= 0 or high_price <= low_price:
                continue
            close_position = (close_price - low_price) / (high_price - low_price) if high_price > low_price else 0.5
            touched_support = low_price <= support_zone_price * 1.02
            if touched_support and close_price >= support_zone_price * 1.003:
                support_hold_days += 1
            if touched_support and (close_price >= open_price or close_position >= 0.58):
                support_rebound_count += 1
    has_support_absorption = (
        support_zone_gap_pct <= 3.6
        and support_hold_days >= 3
        and support_rebound_count >= 2
        and lower_shadow_absorption >= 0.28
    )

    prev_lows = [_safe_float(item.get("low")) for item in completed_bars[-6:-3] if _safe_float(item.get("low")) > 0]
    recent_lows = [_safe_float(item.get("low")) for item in recent_three if _safe_float(item.get("low")) > 0]
    higher_low = bool(prev_lows and recent_lows and min(recent_lows) >= min(prev_lows) * 0.99)
    has_platform_contraction = bool(platform_context.get("has_platform_contraction"))
    has_volume_contraction = bool(platform_context.get("has_volume_contraction"))
    has_volume_suffocation = bool(platform_context.get("has_volume_suffocation"))
    near_breakout = bool(platform_context.get("near_breakout"))
    has_doji_confirmation = recent_doji_count >= 1
    closes = [_safe_float(item.get("close")) for item in completed_bars if _safe_float(item.get("close")) > 0]
    highs = [_safe_float(item.get("high")) for item in completed_bars if _safe_float(item.get("high")) > 0]
    volumes = [_safe_float(item.get("volume")) for item in completed_bars if _safe_float(item.get("volume")) >= 0]
    ma5 = _mean(closes[-5:]) if len(closes) >= 5 else 0.0
    ma10 = _mean(closes[-10:]) if len(closes) >= 10 else 0.0
    ma20 = _mean(closes[-20:]) if len(closes) >= 20 else 0.0
    reference_trend_high = max(highs[:-1]) if len(highs) >= 2 else 0.0
    trend_breakout_gap_pct = _safe_percent_change(latest_close, reference_trend_high) if reference_trend_high > 0 else 0.0
    near_trend_high = bool(reference_trend_high > 0 and latest_close >= reference_trend_high * 0.985)
    trend_breakout = bool(reference_trend_high > 0 and latest_close >= reference_trend_high * 1.005)
    ma_alignment = bool(
        ma5 > 0
        and latest_close >= ma5 * 0.99
        and (
            (
                ma10 > 0
                and ma5 >= ma10 * 0.995
                and (ma20 <= 0 or ma10 >= ma20 * 0.985)
            )
            or (
                ma10 <= 0
                and len(closes) >= 5
                and closes[-1] >= closes[-5] * 1.01
            )
        )
    )
    previous_volume = _mean(volumes[-8:-3]) if len(volumes) >= 8 else _mean(volumes[:-3])
    recent_volume = _mean(volumes[-3:]) if len(volumes) >= 3 else 0.0
    trend_volume_release = bool(previous_volume > 0 and 1.05 <= recent_volume / previous_volume <= 2.8)
    trend_acceleration_ready = bool(
        trend_breakout
        and ma_alignment
        and higher_low
        and trend_volume_release
        and failed_reversal_count == 0
        and overhead_gap_pct <= 5.5
    )
    platform_confirmation_count = sum(
        1
        for flag in (
            has_platform_contraction,
            (has_volume_contraction or has_volume_suffocation),
            near_breakout,
            higher_low,
        )
        if flag
    )
    trend_confirmation_count = sum(
        1
        for flag in (
            near_trend_high,
            trend_breakout,
            ma_alignment,
            trend_volume_release,
            higher_low,
        )
        if flag
    )
    strict_confirmation_count = max(
        platform_confirmation_count,
        trend_confirmation_count,
        3 if burst_pullback_ready else 0,
    )
    qualified_doji_confirmation = (
        has_doji_confirmation
        and strict_confirmation_count >= 2
        and failed_reversal_count == 0
        and overhead_gap_pct <= 3.5
    )
    strict_ready = failed_reversal_count == 0 and (
        (platform_confirmation_count >= 2 and overhead_gap_pct <= 4.0)
        or trend_acceleration_ready
        or burst_pullback_ready
    )

    bonus = 0.0
    penalty = 0.0
    if qualified_doji_confirmation:
        bonus += 0.02
    elif has_doji_confirmation:
        penalty += 0.01
    if has_platform_contraction:
        bonus += 0.06
    elif platform_range_pct >= 14:
        penalty += 0.04
    if has_volume_suffocation:
        bonus += 0.08
    elif has_volume_contraction:
        bonus += 0.06
    elif volume_shrink_ratio >= 1.12:
        penalty += 0.04
    if near_breakout:
        bonus += 0.05
    else:
        penalty += 0.02
    if higher_low:
        bonus += 0.03
    if near_trend_high:
        bonus += 0.02
    if trend_acceleration_ready:
        bonus += 0.05
    elif trend_breakout and ma_alignment:
        bonus += 0.035
    elif near_trend_high and ma_alignment:
        bonus += 0.015
    if burst_pullback_ready:
        bonus += 0.045 if _safe_float(burst_pullback_meta.get("burst_pullback_score")) >= 70.0 else 0.035
    if strict_confirmation_count < 2:
        penalty += 0.04
    elif strict_confirmation_count >= 3:
        bonus += 0.03
    if has_volume_suffocation and str(platform_context.get("platform_cycle_type") or "") == "long":
        bonus += 0.02
    if failed_reversal_count >= 1:
        penalty += 0.06
    if overhead_gap_pct >= 5.5:
        penalty += 0.03
    if latest_is_provisional:
        penalty += 0.04

    confirmation_score = max(0.0, min(1.0, 0.45 + bonus - penalty))
    context = {
        "confirmation_bonus": round(min(bonus, 0.16), 3),
        "confirmation_penalty": round(min(penalty, 0.16), 3),
        "confirmation_score": round(confirmation_score, 3),
        "has_doji_confirmation": has_doji_confirmation,
        "qualified_doji_confirmation": qualified_doji_confirmation,
        "has_platform_contraction": has_platform_contraction,
        "has_volume_contraction": has_volume_contraction,
        "has_volume_suffocation": has_volume_suffocation,
        "near_breakout": near_breakout,
        "failed_reversal_count": failed_reversal_count,
        "platform_cycle_type": str(platform_context.get("platform_cycle_type") or "short"),
        "platform_cycle_label": str(platform_context.get("platform_cycle_label") or "短平台"),
        "platform_cycle_days": _safe_int(platform_context.get("platform_cycle_days")),
        "platform_range_pct": round(platform_range_pct, 2),
        "volume_shrink_ratio": round(volume_shrink_ratio, 3),
        "volume_suffocation_ratio": round(volume_suffocation_ratio, 3),
        "recent_volume_ceiling_ratio": round(recent_volume_ceiling_ratio, 3),
        "recent_amplitude_pct": round(recent_amplitude_pct, 2),
        "latest_close": round(latest_close, 2),
        "platform_reference_high": round(_safe_float(platform_context.get("platform_reference_high")), 2),
        "overhead_gap_pct": round(overhead_gap_pct, 2),
        "support_zone_price": round(support_zone_price, 2) if support_zone_price > 0 else 0.0,
        "support_zone_gap_pct": round(support_zone_gap_pct, 2) if support_zone_gap_pct < 999.0 else 999.0,
        "support_hold_days": support_hold_days,
        "support_rebound_count": support_rebound_count,
        "lower_shadow_absorption": round(lower_shadow_absorption, 3),
        "has_support_absorption": has_support_absorption,
        "near_trend_high": near_trend_high,
        "trend_breakout": trend_breakout,
        "trend_acceleration_ready": trend_acceleration_ready,
        "ma_alignment": ma_alignment,
        "trend_volume_release": trend_volume_release,
        "trend_breakout_gap_pct": round(trend_breakout_gap_pct, 2),
        "platform_confirmation_count": platform_confirmation_count,
        "trend_confirmation_count": trend_confirmation_count,
        "ma5": round(ma5, 3),
        "ma10": round(ma10, 3),
        "ma20": round(ma20, 3),
        "strict_confirmation_count": strict_confirmation_count,
        "strict_ready": strict_ready,
        "provisional_last_bar": latest_is_provisional,
        "insufficient_history": False,
        "long_cycle_profile": long_cycle_profile,
        **launch_profile,
        **burst_pullback_meta,
    }
    context["summary"] = _summarize_kline_confirmation(context)
    if latest_is_provisional:
        context["summary"] = f"{context['summary']} / 盘中K线未收盘降权"
    return context


def _build_platform_relaunch_signal_meta(
    kline_context: dict | None,
    memory_features: dict | None,
) -> dict:
    kline_context = kline_context or {}
    memory_features = memory_features or {}

    memory_hits = _safe_int(memory_features.get("memory_limit_up_hits_50d"))
    days_since_last = _safe_int(memory_features.get("memory_days_since_last_limit_up"), 999)
    latest_close = _safe_float(kline_context.get("latest_close"))
    last_limit_up_close = _safe_float(memory_features.get("memory_last_limit_up_close"))
    last_limit_up_high = _safe_float(memory_features.get("memory_last_limit_up_high"))
    anchor_price = max(last_limit_up_high, last_limit_up_close)
    anchor_gap_pct = _safe_absolute_percent_gap(latest_close, anchor_price)
    has_recent_limit_up_event = (
        memory_hits >= 1
        and PLATFORM_RELAUNCH_MIN_DAYS_SINCE_LAST_LIMIT_UP <= days_since_last <= PLATFORM_RELAUNCH_MAX_DAYS_SINCE_LAST_LIMIT_UP
    )
    near_anchor = has_recent_limit_up_event and anchor_gap_pct <= PLATFORM_RELAUNCH_MAX_ANCHOR_GAP_PCT
    limit_up_nearby_doji_confirmation = (
        near_anchor
        and bool(kline_context.get("has_doji_confirmation"))
        and _safe_int(kline_context.get("failed_reversal_count")) == 0
        and (
            bool(kline_context.get("qualified_doji_confirmation"))
            or (
                _safe_int(kline_context.get("strict_confirmation_count")) >= 2
                and bool(kline_context.get("near_breakout"))
            )
        )
        and anchor_gap_pct <= PLATFORM_RELAUNCH_DOJI_MAX_ANCHOR_GAP_PCT
    )
    platform_relaunch_event_ready = near_anchor and (
        limit_up_nearby_doji_confirmation
        or (
            (bool(kline_context.get("has_platform_contraction")) or bool(kline_context.get("has_volume_suffocation")))
            and bool(kline_context.get("near_breakout"))
        )
    )
    signal_score = _clamp_score(
        (35.0 if has_recent_limit_up_event else 0.0)
        + _inverse_score(anchor_gap_pct, good=1.2, bad=6.0) * 0.35
        + (20.0 if limit_up_nearby_doji_confirmation else 0.0)
        + (10.0 if bool(kline_context.get("has_platform_contraction")) else 0.0)
        + (10.0 if bool(kline_context.get("has_volume_suffocation")) else 0.0)
        + min(_safe_int(kline_context.get("strict_confirmation_count")), 4) / 4.0 * 10.0
    )
    pattern_label = ""
    if limit_up_nearby_doji_confirmation:
        pattern_label = "涨停板附近十字星"
    elif platform_relaunch_event_ready:
        pattern_label = "涨停后板附近横盘"

    return {
        "has_recent_limit_up_event": has_recent_limit_up_event,
        "platform_relaunch_event_ready": platform_relaunch_event_ready,
        "memory_last_limit_up_anchor_price": round(anchor_price, 2) if anchor_price > 0 else 0.0,
        "limit_up_anchor_gap_pct": round(anchor_gap_pct, 2) if anchor_gap_pct < 999.0 else 999.0,
        "limit_up_nearby_doji_confirmation": limit_up_nearby_doji_confirmation,
        "limit_up_nearby_signal_score": signal_score,
        "limit_up_nearby_pattern_label": pattern_label,
    }


def _build_support_squeeze_signal_meta(
    kline_context: dict | None,
    detail: dict | None = None,
) -> dict:
    kline_context = kline_context or {}
    detail = detail or {}

    support_zone_gap_pct = _safe_float(kline_context.get("support_zone_gap_pct"), 999.0)
    support_zone_price = _safe_float(kline_context.get("support_zone_price"))
    support_hold_days = _safe_int(kline_context.get("support_hold_days"))
    support_rebound_count = _safe_int(kline_context.get("support_rebound_count"))
    lower_shadow_absorption = _safe_float(kline_context.get("lower_shadow_absorption"))
    has_support_absorption = bool(kline_context.get("has_support_absorption"))
    has_volume_suffocation = bool(kline_context.get("has_volume_suffocation"))
    has_volume_contraction = bool(kline_context.get("has_volume_contraction"))
    strict_confirmation_count = _safe_int(kline_context.get("strict_confirmation_count"))
    failed_reversal_count = _safe_int(kline_context.get("failed_reversal_count"))
    overhead_gap_pct = _safe_float(kline_context.get("overhead_gap_pct"))
    volume_ratio = _safe_float(detail.get("volume_ratio"))
    change_pct = _safe_float(detail.get("change_pct"))
    support_strength = _safe_float(detail.get("support_strength_score"))

    support_squeeze_only_ready = (
        support_zone_price > 0
        and support_zone_gap_pct <= 3.2
        and has_support_absorption
        and has_volume_suffocation
        and strict_confirmation_count >= 2
        and failed_reversal_count == 0
        and overhead_gap_pct >= 1.2
    )
    support_volume_release_ready = (
        support_zone_price > 0
        and support_zone_gap_pct <= 3.2
        and has_support_absorption
        and SUPPORT_VOLUME_RELEASE_MIN_RATIO <= volume_ratio <= SUPPORT_VOLUME_RELEASE_MAX_RATIO
        and SUPPORT_VOLUME_RELEASE_MIN_CHANGE_PCT <= change_pct <= SUPPORT_VOLUME_RELEASE_MAX_CHANGE_PCT
        and support_strength >= SUPPORT_VOLUME_RELEASE_MIN_SUPPORT_STRENGTH
        and strict_confirmation_count >= 2
        and failed_reversal_count == 0
    )
    squeeze_signal_score = _clamp_score(
        _inverse_score(support_zone_gap_pct, good=1.0, bad=5.0) * 0.34
        + min(support_hold_days, 5) / 5.0 * 18.0
        + min(support_rebound_count, 4) / 4.0 * 16.0
        + min(lower_shadow_absorption, 0.8) / 0.8 * 18.0
        + (10.0 if has_volume_suffocation else 6.0 if has_volume_contraction else 0.0)
        + (4.0 if strict_confirmation_count >= 3 else 0.0)
    )
    support_volume_release_signal_score = _clamp_score(
        _inverse_score(support_zone_gap_pct, good=1.0, bad=5.0) * 0.26
        + min(support_hold_days, 5) / 5.0 * 16.0
        + min(support_rebound_count, 4) / 4.0 * 14.0
        + min(lower_shadow_absorption, 0.8) / 0.8 * 14.0
        + _range_score(
            volume_ratio,
            ideal_low=SUPPORT_VOLUME_RELEASE_MIN_RATIO,
            ideal_high=2.2,
            min_value=0.5,
            max_value=3.6,
        ) * 0.16
        + _range_score(
            change_pct,
            ideal_low=SUPPORT_VOLUME_RELEASE_MIN_CHANGE_PCT,
            ideal_high=4.8,
            min_value=-1.0,
            max_value=8.2,
        ) * 0.08
        + min(support_strength, 100.0) * 0.12
        + (6.0 if strict_confirmation_count >= 3 else 0.0)
    )
    support_squeeze_ready = support_squeeze_only_ready or support_volume_release_ready
    signal_score = max(squeeze_signal_score, support_volume_release_signal_score)
    pattern_label = ""
    if support_volume_release_ready:
        pattern_label = "低位支撑放量启动"
    elif support_squeeze_only_ready:
        pattern_label = "低位支撑量窒息"
    elif has_support_absorption and volume_ratio >= 1.05:
        pattern_label = "支撑位放量预热"
    elif has_support_absorption:
        pattern_label = "支撑位缩量蓄势"

    return {
        "support_squeeze_ready": support_squeeze_ready,
        "support_squeeze_only_ready": support_squeeze_only_ready,
        "support_squeeze_signal_score": signal_score,
        "support_volume_release_ready": support_volume_release_ready,
        "support_volume_release_signal_score": support_volume_release_signal_score,
        "support_squeeze_pattern_label": pattern_label,
        "support_zone_price": round(support_zone_price, 2) if support_zone_price > 0 else 0.0,
        "support_zone_gap_pct": round(support_zone_gap_pct, 2) if support_zone_gap_pct < 999.0 else 999.0,
        "support_hold_days": support_hold_days,
        "support_rebound_count": support_rebound_count,
        "lower_shadow_absorption": round(lower_shadow_absorption, 3),
        "has_support_absorption": has_support_absorption,
    }


def _passes_strict_first_board_gate(
    row: dict,
    *,
    bull_score: float,
    sector: dict,
    kline_context: dict,
    memory_features: dict | None = None,
    market_context: dict | None = None,
) -> tuple[bool, list[str], str]:
    blockers: list[str] = []
    event_types = {str(item or "") for item in row.get("event_types") or []}
    detail = row.get("detail") or {}
    memory_features = memory_features or {}
    sector_strength = _safe_float(sector.get("strength_score"))
    sector_continuity = _calc_sector_continuity_score(sector)
    sector_rotation_score = _safe_float(sector.get("sector_rotation_score"))
    has_low_position_rotation = bool(sector.get("sector_low_position_rotation"))
    has_crowded_stale_theme = bool(sector.get("sector_crowded_stale_theme"))
    support_strength = _safe_float(detail.get("support_strength_score"))
    main_inflow_pct = _safe_float(detail.get("main_net_inflow_pct"))
    volume_ratio = _safe_float(detail.get("volume_ratio"))
    change_pct = _safe_float(detail.get("change_pct"))
    amplitude = _safe_float(detail.get("amplitude"))
    kline_score = _safe_float(kline_context.get("confirmation_score"))
    strict_ready = bool(kline_context.get("strict_ready"))
    strict_confirmation_count = _safe_int(kline_context.get("strict_confirmation_count"))
    provisional_last_bar = bool(kline_context.get("provisional_last_bar"))
    failed_reversal_count = _safe_int(kline_context.get("failed_reversal_count"))
    overhead_gap_pct = _safe_float(kline_context.get("overhead_gap_pct"))
    has_platform_contraction = bool(kline_context.get("has_platform_contraction"))
    has_volume_contraction = bool(kline_context.get("has_volume_contraction"))
    has_volume_suffocation = bool(kline_context.get("has_volume_suffocation"))
    near_breakout = bool(kline_context.get("near_breakout"))
    qualified_doji = bool(kline_context.get("qualified_doji_confirmation"))
    memory_score = _safe_float(memory_features.get("memory_score"))
    platform_relaunch_meta = _build_platform_relaunch_signal_meta(kline_context, memory_features)
    support_squeeze_meta = _build_support_squeeze_signal_meta(kline_context, detail)
    platform_relaunch_event_ready = bool(platform_relaunch_meta.get("platform_relaunch_event_ready"))
    limit_up_nearby_doji = bool(platform_relaunch_meta.get("limit_up_nearby_doji_confirmation"))
    has_recent_limit_up_event = bool(platform_relaunch_meta.get("has_recent_limit_up_event"))
    support_squeeze_ready = bool(support_squeeze_meta.get("support_squeeze_ready"))
    support_volume_release_ready = bool(support_squeeze_meta.get("support_volume_release_ready"))
    broad_rotation_member_setup = bool(detail.get("broad_rotation_member_setup"))
    sector_catalyst_spread = bool(detail.get("sector_catalyst_spread"))
    broad_rotation_cluster_setup = bool(detail.get("broad_rotation_cluster_setup")) or _is_broad_rotation_cluster_sector(sector, market_context)
    sector_catalyst_spread = bool(detail.get("sector_catalyst_spread"))
    has_news_catalyst_trigger = "news_catalyst" in event_types
    has_auction_surge_trigger = "auction_surge" in event_types
    auction_feed_complete = auction_context_complete(detail)
    has_mainline_spread_trigger = "mainline_spread" in event_types
    has_pre_board_probe_trigger = "pre_board_probe" in event_types
    has_oversold_reversal_trigger = "oversold_reversal" in event_types
    has_primary_trigger = bool(
        event_types.intersection({"capital", "breakthrough"})
        or has_news_catalyst_trigger
        or has_auction_surge_trigger
        or has_mainline_spread_trigger
        or has_pre_board_probe_trigger
        or has_oversold_reversal_trigger
    )
    has_mainline_relay_trigger = "mainline_relay" in event_types
    has_stealth_trigger = "rapid_rise" in event_types
    has_quiet_trigger = "stealth_setup" in event_types
    trend_acceleration_ready = bool(kline_context.get("trend_acceleration_ready"))
    route = ""
    broad_rotation_relief = (
        (bool((market_context or {}).get("broad_first_board_overflow")) or sector_catalyst_spread)
        and has_mainline_spread_trigger
        and (sector_strength >= BROAD_ROTATION_MAINLINE_MIN_SECTOR_STRENGTH or broad_rotation_cluster_setup)
        and (
            sector_continuity >= BROAD_ROTATION_MAINLINE_MIN_SECTOR_CONTINUITY
            or (
                has_low_position_rotation
                and sector_rotation_score >= BROAD_ROTATION_MAINLINE_MIN_ROTATION_SCORE
            )
            or _safe_float(sector.get("sector_limit_up_delta")) >= 2.0
            or broad_rotation_cluster_setup
        )
        and support_strength >= (36.0 if broad_rotation_cluster_setup else 42.0)
        and volume_ratio >= (0.35 if broad_rotation_cluster_setup else 0.4 if broad_rotation_member_setup else 0.55)
    )

    if has_primary_trigger:
        is_fresh_hot_start = memory_score < FRESH_HOT_FIRST_BOARD_MAX_MEMORY_SCORE
        if has_auction_surge_trigger:
            route = "strict_auction_surge"
            min_sector_strength = 18.0
            min_sector_continuity = 0.06
            min_support_strength = 42.0
            min_bull_score = 46.0
        elif has_news_catalyst_trigger:
            route = "strict_news_catalyst"
            min_sector_strength = 18.0
            min_sector_continuity = 0.06
            min_support_strength = 38.0
            min_bull_score = 48.0
        elif has_mainline_spread_trigger:
            route = "strict_mainline_spread"
            min_sector_strength = MAINLINE_SPREAD_MIN_SECTOR_STRENGTH
            min_sector_continuity = MAINLINE_SPREAD_MIN_SECTOR_CONTINUITY
            min_support_strength = 46.0
            min_bull_score = 50.0
        elif has_pre_board_probe_trigger:
            route = "strict_pre_board_probe"
            min_sector_strength = 12.0
            min_sector_continuity = 0.03
            min_support_strength = 42.0
            min_bull_score = 48.0
        elif has_oversold_reversal_trigger:
            route = "strict_oversold_reversal"
            min_sector_strength = 0.0
            min_sector_continuity = 0.0
            min_support_strength = 38.0
            min_bull_score = 46.0
        else:
            route = "strict_hot_fresh" if is_fresh_hot_start else "strict_hot"
            min_sector_strength = (
                FRESH_HOT_FIRST_BOARD_MIN_SECTOR_STRENGTH if is_fresh_hot_start else STRICT_FIRST_BOARD_MIN_SECTOR_STRENGTH
            )
            min_sector_continuity = (
                FRESH_HOT_FIRST_BOARD_MIN_SECTOR_CONTINUITY if is_fresh_hot_start else STRICT_FIRST_BOARD_MIN_SECTOR_CONTINUITY
            )
            min_support_strength = (
                FRESH_HOT_FIRST_BOARD_MIN_SUPPORT_STRENGTH if is_fresh_hot_start else STRICT_FIRST_BOARD_MIN_SUPPORT_STRENGTH
            )
            min_bull_score = (
                FRESH_HOT_FIRST_BOARD_MIN_BULL_SCORE if is_fresh_hot_start else STRICT_FIRST_BOARD_MIN_BULL_SCORE
            )
        mainline_relay_relief = (
            has_mainline_relay_trigger
            and sector_strength >= HOT_MAINLINE_RELAY_MIN_SECTOR_STRENGTH
            and sector_continuity >= 0.22
            and (
                support_strength >= FRESH_HOT_FIRST_BOARD_MIN_SUPPORT_STRENGTH
                or main_inflow_pct >= 3.0
            )
            and (trend_acceleration_ready or near_breakout)
        )
        rotation_relief = (
            has_low_position_rotation
            and sector_rotation_score >= 58.0
            and (
                has_mainline_spread_trigger
                or has_pre_board_probe_trigger
                or has_auction_surge_trigger
                or has_news_catalyst_trigger
                or is_fresh_hot_start
            )
        )
        if broad_rotation_relief:
            if broad_rotation_cluster_setup:
                min_sector_strength = max(min_sector_strength - 44.0, 18.0)
                min_sector_continuity = 0.0
                min_support_strength = max(min_support_strength - 10.0, 36.0)
            else:
                min_sector_strength = max(min_sector_strength - 12.0, BROAD_ROTATION_MAINLINE_MIN_SECTOR_STRENGTH)
                min_sector_continuity = max(
                    min_sector_continuity - 0.08,
                    BROAD_ROTATION_MAINLINE_MIN_SECTOR_CONTINUITY,
                )
                min_support_strength = max(min_support_strength - 8.0, 38.0)
        if rotation_relief:
            min_sector_strength = max(min_sector_strength - 8.0, 12.0)
            min_sector_continuity = max(min_sector_continuity - 0.04, 0.0)
        effective_min_bull_score = max(min_bull_score - (10.0 if mainline_relay_relief else 0.0), 48.0)
        if rotation_relief:
            effective_min_bull_score = max(effective_min_bull_score - 4.0, 44.0)
        if broad_rotation_relief:
            effective_min_bull_score = max(effective_min_bull_score - 4.0, 44.0)

        if sector_strength < min_sector_strength:
            blockers.append("主线强度不足")
        if sector_continuity < min_sector_continuity:
            blockers.append("题材持续性不足")
        if support_strength < min_support_strength:
            blockers.append("承接分不足")
        if bull_score < effective_min_bull_score:
            blockers.append("牛股分不足")
        news_independent_trigger = bool(
            has_news_catalyst_trigger
            and _safe_float(detail.get("news_catalyst_score")) >= NEWS_CATALYST_MIN_SCORE + 10.0
            and (support_strength >= 36.0 or sector_strength >= 28.0)
            and -1.2 <= change_pct <= 9.2
            and main_inflow_pct >= -5.0
        )
        auction_independent_trigger = bool(
            has_auction_surge_trigger
            and _safe_float(detail.get("auction_strength_score")) >= AUCTION_SURGE_MIN_SCORE + 8.0
            and _safe_float(detail.get("auction_open_change")) >= AUCTION_SURGE_MIN_OPEN_CHANGE
            and _safe_float(detail.get("auction_open_change")) < AUCTION_SURGE_MAX_OPEN_CHANGE
            and auction_feed_complete
            and (support_strength >= 38.0 or sector_strength >= 25.0)
            and 0.0 <= change_pct <= 9.2
            and not bool(detail.get("auction_cancelled"))
        )
        mainline_min_change_pct = -6.5 if broad_rotation_cluster_setup else -2.5 if broad_rotation_relief else MAINLINE_SPREAD_MIN_CHANGE_PCT
        mainline_independent_trigger = bool(
            has_mainline_spread_trigger
            and (
                broad_rotation_relief
                or (
                    sector_strength >= MAINLINE_SPREAD_MIN_SECTOR_STRENGTH
                    and sector_continuity >= MAINLINE_SPREAD_MIN_SECTOR_CONTINUITY
                )
            )
            and (support_strength >= 38.0 or main_inflow_pct >= -1.5)
            and mainline_min_change_pct <= change_pct <= MAINLINE_SPREAD_MAX_CHANGE_PCT
            and volume_ratio >= (
                0.4
                if broad_rotation_cluster_setup or (broad_rotation_member_setup and broad_rotation_relief)
                else 0.55
                if broad_rotation_relief
                else HOT_MAINLINE_RELAY_MIN_VOLUME_RATIO
            )
        )
        route_independent_trigger = bool(
            news_independent_trigger
            or auction_independent_trigger
            or mainline_independent_trigger
        )
        min_confirmation_count = 1 if (
            is_fresh_hot_start
            or has_news_catalyst_trigger
            or has_auction_surge_trigger
            or has_mainline_spread_trigger
            or has_pre_board_probe_trigger
            or has_oversold_reversal_trigger
        ) else 2
        effective_confirmation_count = (
            0
            if route_independent_trigger
            else 1
            if (not is_fresh_hot_start and platform_relaunch_event_ready)
            else min_confirmation_count
        )
        structure_ready = (
            route_independent_trigger
            or
            strict_ready
            or (not is_fresh_hot_start and limit_up_nearby_doji)
            or (
                (
                    has_news_catalyst_trigger
                    or has_auction_surge_trigger
                    or has_mainline_spread_trigger
                    or has_pre_board_probe_trigger
                    or has_oversold_reversal_trigger
                )
                and (
                    strict_confirmation_count >= 1
                    or bool(kline_context.get("near_trend_high"))
                    or near_breakout
                    or trend_acceleration_ready
                    or _safe_float(detail.get("pre_board_probe_score")) >= PRE_BOARD_PROBE_MIN_SCORE
                    or _safe_float(detail.get("oversold_reversal_score")) >= OVERSOLD_REVERSAL_MIN_SCORE
                )
            )
        )
        if strict_confirmation_count < effective_confirmation_count or not structure_ready:
            blockers.append("K线结构共振不足")
        pre_board_reversal_relief = bool(
            has_pre_board_probe_trigger
            and _safe_float(detail.get("pre_board_probe_score")) >= PRE_BOARD_PROBE_MIN_SCORE + 8.0
            and support_strength >= 70.0
            and (bull_score >= 70.0 or sector_strength >= 55.0)
            and volume_ratio >= 0.65
            and change_pct >= 0.0
            and main_inflow_pct >= -8.0
        )
        mainline_reversal_relief = bool(
            has_mainline_spread_trigger
            and broad_rotation_relief
            and (sector_strength >= 55.0 or broad_rotation_cluster_setup)
            and volume_ratio >= 0.75
            and main_inflow_pct >= -6.5
        )
        news_reversal_relief = bool(
            has_news_catalyst_trigger
            and _safe_float(detail.get("news_catalyst_score")) >= NEWS_CATALYST_MIN_SCORE + 14.0
            and support_strength >= 55.0
            and (sector_strength >= 50.0 or bull_score >= 68.0)
            and main_inflow_pct >= -5.0
        )
        failed_reversal_relief = bool(
            pre_board_reversal_relief
            or mainline_reversal_relief
            or news_reversal_relief
        )
        if failed_reversal_count > 0 and not has_oversold_reversal_trigger and not failed_reversal_relief:
            blockers.append("上冲回落风险")
        if has_news_catalyst_trigger:
            if _safe_float(detail.get("news_catalyst_score")) < NEWS_CATALYST_MIN_SCORE:
                blockers.append("消息催化强度不足")
            if change_pct < -1.0 or change_pct >= 9.2:
                blockers.append("涨幅区间不符合消息催化")
            if main_inflow_pct < -4.0:
                blockers.append("消息后资金明显背离")
        elif has_auction_surge_trigger:
            if _safe_float(detail.get("auction_strength_score")) < AUCTION_SURGE_MIN_SCORE:
                blockers.append("竞价强度不足")
            if _safe_float(detail.get("auction_open_change")) < AUCTION_SURGE_MIN_OPEN_CHANGE:
                blockers.append("竞价高开不足")
            if _safe_float(detail.get("auction_open_change")) >= AUCTION_SURGE_MAX_OPEN_CHANGE:
                blockers.append("竞价高开过度，次日赔率不足")
            if not auction_feed_complete:
                blockers.append("竞价增量成交未确认")
            if bool(detail.get("auction_cancelled")):
                blockers.append("竞价撤单风险")
            if change_pct < 0.0 or change_pct >= 9.2:
                blockers.append("涨幅区间不符合竞价强攻")
        elif has_mainline_spread_trigger:
            effective_spread_sector_strength = (
                18.0
                if broad_rotation_cluster_setup
                else BROAD_ROTATION_MAINLINE_MIN_SECTOR_STRENGTH
                if broad_rotation_relief
                else MAINLINE_SPREAD_MIN_SECTOR_STRENGTH
            )
            if sector_strength < effective_spread_sector_strength:
                blockers.append("主线扩散强度不足")
            if not (mainline_min_change_pct <= change_pct <= MAINLINE_SPREAD_MAX_CHANGE_PCT):
                blockers.append("涨幅区间不符合主线扩散")
            min_mainline_volume_ratio = (
                0.4
                if broad_rotation_cluster_setup or (broad_rotation_member_setup and broad_rotation_relief)
                else 0.55
                if broad_rotation_relief
                else HOT_MAINLINE_RELAY_MIN_VOLUME_RATIO
            )
            if volume_ratio < min_mainline_volume_ratio:
                blockers.append("主线扩散量能不足")
            if main_inflow_pct < (-6.5 if broad_rotation_cluster_setup else -2.0):
                blockers.append("主线扩散资金背离")
        elif has_pre_board_probe_trigger:
            if _safe_float(detail.get("pre_board_probe_score")) < PRE_BOARD_PROBE_MIN_SCORE:
                blockers.append("涨停试盘强度不足")
            if change_pct < -2.0 or change_pct >= 8.9:
                blockers.append("涨幅区间不符合涨停试盘")
            pre_board_volume_relief = bool(
                _safe_float(detail.get("pre_board_probe_score")) >= PRE_BOARD_PROBE_MIN_SCORE + 10.0
                and volume_ratio >= 0.65
                and support_strength >= 70.0
                and (sector_strength >= 55.0 or bull_score >= 72.0)
            )
            if volume_ratio < 0.8 and not pre_board_volume_relief:
                blockers.append("试盘量能不足")
            if _safe_float(detail.get("intraday_high_pct")) < 4.0 and not bool(detail.get("preheat_breakout")):
                blockers.append("盘中冲高力度不足")
        elif has_oversold_reversal_trigger:
            if _safe_float(detail.get("oversold_reversal_score")) < OVERSOLD_REVERSAL_MIN_SCORE:
                blockers.append("跌后反包强度不足")
            if change_pct > -1.0 or change_pct < -10.5:
                blockers.append("跌幅区间不符合反包")
            if volume_ratio < 0.6:
                blockers.append("反包前量能不足")
            if _safe_float(detail.get("upper_gap_pct")) < 1.2 and not bool(detail.get("panic_flush")):
                blockers.append("恐慌释放不足")
        elif is_fresh_hot_start:
            if "capital" in event_types and main_inflow_pct < FRESH_HOT_FIRST_BOARD_MIN_MAIN_INFLOW_PCT:
                blockers.append("热启动触发强度不足")
            if "breakthrough" in event_types and volume_ratio < FRESH_HOT_FIRST_BOARD_MIN_BREAKTHROUGH_VOLUME_RATIO:
                blockers.append("热启动触发强度不足")
            if change_pct < FRESH_HOT_FIRST_BOARD_MIN_CHANGE_PCT or change_pct >= FRESH_HOT_FIRST_BOARD_MAX_CHANGE_PCT:
                blockers.append("涨幅区间不符合热启动")
        else:
            if "capital" in event_types and main_inflow_pct < 5.0:
                blockers.append("资金流入不够强")
            if "breakthrough" in event_types and volume_ratio < 1.3:
                blockers.append("突破量能不足")
            if change_pct >= 8.5:
                blockers.append("涨幅过大接近板上")

        min_kline_score = (
            FRESH_HOT_FIRST_BOARD_MIN_INTRADAY_KLINE_SCORE
            if provisional_last_bar and is_fresh_hot_start
            else FRESH_HOT_FIRST_BOARD_MIN_KLINE_SCORE
            if is_fresh_hot_start
            else STRICT_FIRST_BOARD_MIN_INTRADAY_KLINE_SCORE
            if provisional_last_bar
            else STRICT_FIRST_BOARD_MIN_KLINE_SCORE
        )
        if not is_fresh_hot_start and platform_relaunch_event_ready:
            min_kline_score = max(min_kline_score - PLATFORM_RELAUNCH_KLINE_SCORE_RELIEF, 0.0)
        if has_news_catalyst_trigger:
            min_kline_score = 0.36
        elif has_auction_surge_trigger:
            min_kline_score = 0.38
        elif has_mainline_spread_trigger:
            min_kline_score = 0.34 if broad_rotation_relief else 0.40
        elif has_pre_board_probe_trigger:
            min_kline_score = 0.30
        elif has_oversold_reversal_trigger:
            min_kline_score = 0.24
        if route_independent_trigger:
            min_kline_score = min(min_kline_score, 0.18)
        if kline_score < min_kline_score:
            blockers.append("K线确认分不足")
        if has_crowded_stale_theme and has_pre_board_probe_trigger and not (has_news_catalyst_trigger or has_auction_surge_trigger):
            blockers.append("旧主线退潮试盘风险")
    elif has_stealth_trigger or has_quiet_trigger:
        is_quiet_setup = has_quiet_trigger and not has_stealth_trigger
        route = "strict_stealth_scan" if is_quiet_setup else "strict_stealth"
        min_sector_strength = (
            QUIET_FIRST_BOARD_MIN_SECTOR_STRENGTH if is_quiet_setup else STEALTH_FIRST_BOARD_MIN_SECTOR_STRENGTH
        )
        min_sector_continuity = (
            QUIET_FIRST_BOARD_MIN_SECTOR_CONTINUITY if is_quiet_setup else STEALTH_FIRST_BOARD_MIN_SECTOR_CONTINUITY
        )
        min_support_strength = (
            QUIET_FIRST_BOARD_MIN_SUPPORT_STRENGTH if is_quiet_setup else STEALTH_FIRST_BOARD_MIN_SUPPORT_STRENGTH
        )
        min_bull_score = (
            QUIET_FIRST_BOARD_MIN_BULL_SCORE if is_quiet_setup else STEALTH_FIRST_BOARD_MIN_BULL_SCORE
        )

        if sector_strength < min_sector_strength:
            blockers.append("主线强度不足")
        if sector_continuity < min_sector_continuity:
            blockers.append("题材持续性不足")
        if support_strength < min_support_strength:
            blockers.append("承接分不足")
        if bull_score < min_bull_score:
            blockers.append("牛股分不足")
        if (strict_confirmation_count < 3 or not strict_ready) and not (limit_up_nearby_doji or support_squeeze_ready):
            blockers.append("静默蓄势结构不足")
        if failed_reversal_count > 0:
            blockers.append("上冲回落风险")
        if not qualified_doji and strict_confirmation_count < 4 and not (limit_up_nearby_doji or support_squeeze_ready):
            blockers.append("十字星/结构确认不够硬")
        if is_quiet_setup:
            has_soft_platform_shape = (
                strict_confirmation_count >= 2
                and near_breakout
                and (has_platform_contraction or has_volume_contraction or qualified_doji or limit_up_nearby_doji)
                and (sector_strength >= 45.0 or bull_score >= 70.0 or support_strength >= 50.0)
            )
            min_volume_ratio = 0.7 if provisional_last_bar else 0.55
            max_volume_ratio = 2.0 if provisional_last_bar else 1.8
            min_change_pct = 0.5 if provisional_last_bar else 0.0
            max_change_pct = 5.8
            if not (min_volume_ratio <= volume_ratio <= max_volume_ratio):
                blockers.append("量能形态不符合静默蓄势")
            if change_pct < min_change_pct or change_pct >= max_change_pct:
                blockers.append("涨幅区间不符合静默蓄势")
            if amplitude >= 6.8:
                blockers.append("振幅过大不符静默横盘")
            if main_inflow_pct < -2.5:
                blockers.append("资金明显背离")
            if overhead_gap_pct > 2.4 and not platform_relaunch_event_ready and not support_squeeze_ready:
                blockers.append("距离前高仍偏远")
            if (
                strict_confirmation_count < 4
                and not (strict_confirmation_count >= 3 and qualified_doji)
                and not limit_up_nearby_doji
                and not support_squeeze_ready
            ):
                blockers.append("静默结构确认不足 4 项")
            if not (
                qualified_doji
                or (has_platform_contraction and has_volume_suffocation and near_breakout)
                or has_soft_platform_shape
                or platform_relaunch_event_ready
                or support_squeeze_ready
            ):
                blockers.append("缺少缩量平台突破前形态")
            if (
                not has_volume_suffocation
                and not support_volume_release_ready
                and not limit_up_nearby_doji
                and not (has_soft_platform_shape and volume_ratio <= 1.25)
            ):
                blockers.append("未形成量窒息")
            has_memory_edge = memory_score >= 28.0 or has_recent_limit_up_event
            has_rotation_edge = has_low_position_rotation and sector_rotation_score >= 58.0 and support_strength >= 48.0
            has_mainline_edge = (
                (bull_score >= 72.0 and support_strength >= 55.0 and sector_strength >= 50.0)
                or (bull_score >= 68.0 and sector_strength >= 62.0 and support_strength >= 46.0)
                or (bull_score >= 66.0 and sector_strength >= 70.0 and sector_continuity >= 0.18 and change_pct >= 1.0)
                or has_rotation_edge
            )
            if not (has_memory_edge or has_mainline_edge or has_soft_platform_shape or support_squeeze_ready):
                blockers.append("缺少首波记忆或主线共振")
        else:
            if not (0.8 <= volume_ratio <= 2.2):
                blockers.append("量能形态不符合静默蓄势")
            if change_pct < 1.0 or change_pct >= 6.8:
                blockers.append("涨幅区间不符合静默蓄势")
            if main_inflow_pct < -1.5:
                blockers.append("资金明显背离")
            if overhead_gap_pct > 2.8 and not platform_relaunch_event_ready:
                blockers.append("距离前高仍偏远")

        if main_inflow_pct < -1.5 and not is_quiet_setup:
            blockers.append("资金明显背离")

        min_kline_score = (
            QUIET_FIRST_BOARD_MIN_INTRADAY_KLINE_SCORE
            if provisional_last_bar and is_quiet_setup
            else QUIET_FIRST_BOARD_MIN_KLINE_SCORE
            if is_quiet_setup
            else STEALTH_FIRST_BOARD_MIN_INTRADAY_KLINE_SCORE
            if provisional_last_bar
            else STEALTH_FIRST_BOARD_MIN_KLINE_SCORE
        )
        if limit_up_nearby_doji:
            min_kline_score = max(min_kline_score - PLATFORM_RELAUNCH_KLINE_SCORE_RELIEF, 0.0)
        if support_squeeze_ready:
            min_kline_score = max(min_kline_score - SUPPORT_BASE_KLINE_SCORE_RELIEF, 0.0)
        if kline_score < min_kline_score:
            blockers.append("K线确认分不足")
    else:
        blockers.append("缺少显性点火或静默蓄势触发")

    long_cycle_profile = dict(kline_context.get("long_cycle_profile") or {})
    if (
        str(long_cycle_profile.get("long_cycle_regime") or "") == "high_overheat"
        and not (has_news_catalyst_trigger or has_auction_surge_trigger)
        and (has_pre_board_probe_trigger or has_quiet_trigger or has_stealth_trigger)
    ):
        blockers.append("250日位置与中短期涨幅过热")

    return len(blockers) == 0, blockers, route


def _classify_first_board_blocker(blocker: str) -> str:
    text = str(blocker or "")
    if not text:
        return "其他约束"
    if "首波记忆" in text:
        return "缺少首波记忆"
    if any(keyword in text for keyword in ("主线强度不足", "主线扩散强度不足", "题材持续性不足", "牛股分不足")):
        return "主线强度不足"
    if any(
        keyword in text
        for keyword in (
            "承接分不足",
            "资金流入不够强",
            "突破量能不足",
            "热启动触发强度不足",
            "竞价强度不足",
            "竞价高开不足",
            "涨停试盘强度不足",
            "试盘量能不足",
            "反包前量能不足",
            "量能形态",
            "资金明显背离",
            "未形成量窒息",
        )
    ):
        return "量能不够"
    if any(
        keyword in text
        for keyword in (
            "K线",
            "结构",
            "十字星",
            "距离前高",
            "缩量平台",
            "振幅过大",
            "涨幅区间",
            "涨幅过大",
            "过热",
            "上冲回落",
            "盘中冲高力度不足",
            "跌后反包强度不足",
            "恐慌释放不足",
            "历史不足",
        )
    ):
        return "K线结构不够"
    return "其他约束"


def _first_board_diagnostic_route(event_types: set[str]) -> str:
    if "auction_surge" in event_types:
        return "strict_auction_surge"
    if "news_catalyst" in event_types:
        return "strict_news_catalyst"
    if "mainline_spread" in event_types:
        return "strict_mainline_spread"
    if "pre_board_probe" in event_types:
        return "strict_pre_board_probe"
    if "oversold_reversal" in event_types:
        return "strict_oversold_reversal"
    if event_types.intersection({"capital", "breakthrough"}):
        return "strict_hot"
    if "rapid_rise" in event_types:
        return "strict_stealth"
    if "stealth_setup" in event_types:
        return "strict_stealth_scan"
    return "strict_hot"


def _should_collect_first_board_diagnostic_from_events(event_types: set[str]) -> bool:
    return bool({str(item or "").strip() for item in event_types}.intersection(FIRST_BOARD_TRIGGER_EVENT_TYPES))


def _first_board_diagnostic_thresholds(route: str, *, provisional_last_bar: bool = False) -> dict[str, float]:
    normalized_route = str(route or "strict_hot")
    if normalized_route == "strict_news_catalyst":
        return {
            "sector_strength": 18.0,
            "sector_continuity": 0.06,
            "support_strength": 38.0,
            "bull_score": 48.0,
            "kline_score": 0.18,
            "strict_confirmation_count": 0.0,
            "news_catalyst_score": NEWS_CATALYST_MIN_SCORE,
            "change_pct_low": -1.0,
            "change_pct_high": 9.2,
            "main_inflow_pct": -4.0,
        }
    if normalized_route == "strict_auction_surge":
        return {
            "sector_strength": 18.0,
            "sector_continuity": 0.06,
            "support_strength": 42.0,
            "bull_score": 46.0,
            "kline_score": 0.18,
            "strict_confirmation_count": 0.0,
            "auction_strength_score": AUCTION_SURGE_MIN_SCORE,
            "auction_open_change": AUCTION_SURGE_MIN_OPEN_CHANGE,
            "change_pct_low": 0.0,
            "change_pct_high": 9.2,
        }
    if normalized_route == "strict_mainline_spread":
        return {
            "sector_strength": MAINLINE_SPREAD_MIN_SECTOR_STRENGTH,
            "sector_continuity": MAINLINE_SPREAD_MIN_SECTOR_CONTINUITY,
            "support_strength": 46.0,
            "bull_score": 50.0,
            "kline_score": 0.18,
            "strict_confirmation_count": 0.0,
            "volume_ratio_low": HOT_MAINLINE_RELAY_MIN_VOLUME_RATIO,
            "change_pct_low": MAINLINE_SPREAD_MIN_CHANGE_PCT,
            "change_pct_high": MAINLINE_SPREAD_MAX_CHANGE_PCT,
            "main_inflow_pct": -2.0,
        }
    if normalized_route == "strict_pre_board_probe":
        return {
            "sector_strength": 12.0,
            "sector_continuity": 0.03,
            "support_strength": 42.0,
            "bull_score": 48.0,
            "kline_score": 0.30,
            "strict_confirmation_count": 1.0,
            "pre_board_probe_score": PRE_BOARD_PROBE_MIN_SCORE,
            "volume_ratio_low": 0.8,
            "change_pct_low": -2.0,
            "change_pct_high": 8.9,
        }
    if normalized_route == "strict_oversold_reversal":
        return {
            "sector_strength": 0.0,
            "sector_continuity": 0.0,
            "support_strength": 38.0,
            "bull_score": 46.0,
            "kline_score": 0.24,
            "strict_confirmation_count": 1.0,
            "oversold_reversal_score": OVERSOLD_REVERSAL_MIN_SCORE,
            "volume_ratio_low": 0.6,
            "change_pct_low": -10.5,
            "change_pct_high": -1.0,
        }
    if normalized_route == "strict_hot_fresh":
        return {
            "sector_strength": FRESH_HOT_FIRST_BOARD_MIN_SECTOR_STRENGTH,
            "sector_continuity": FRESH_HOT_FIRST_BOARD_MIN_SECTOR_CONTINUITY,
            "support_strength": FRESH_HOT_FIRST_BOARD_MIN_SUPPORT_STRENGTH,
            "bull_score": FRESH_HOT_FIRST_BOARD_MIN_BULL_SCORE,
            "kline_score": (
                FRESH_HOT_FIRST_BOARD_MIN_INTRADAY_KLINE_SCORE
                if provisional_last_bar
                else FRESH_HOT_FIRST_BOARD_MIN_KLINE_SCORE
            ),
            "strict_confirmation_count": 1.0,
            "main_inflow_pct": FRESH_HOT_FIRST_BOARD_MIN_MAIN_INFLOW_PCT,
            "breakthrough_volume_ratio": FRESH_HOT_FIRST_BOARD_MIN_BREAKTHROUGH_VOLUME_RATIO,
            "change_pct_low": FRESH_HOT_FIRST_BOARD_MIN_CHANGE_PCT,
            "change_pct_high": FRESH_HOT_FIRST_BOARD_MAX_CHANGE_PCT,
        }
    if normalized_route == "strict_stealth_scan":
        return {
            "sector_strength": QUIET_FIRST_BOARD_MIN_SECTOR_STRENGTH,
            "sector_continuity": QUIET_FIRST_BOARD_MIN_SECTOR_CONTINUITY,
            "support_strength": QUIET_FIRST_BOARD_MIN_SUPPORT_STRENGTH,
            "bull_score": QUIET_FIRST_BOARD_MIN_BULL_SCORE,
            "kline_score": QUIET_FIRST_BOARD_MIN_INTRADAY_KLINE_SCORE if provisional_last_bar else QUIET_FIRST_BOARD_MIN_KLINE_SCORE,
            "strict_confirmation_count": 4.0,
            "memory_score": 28.0,
            "mainline_sector_strength": 50.0,
            "mainline_support_strength": 55.0,
            "mainline_bull_score": 72.0,
            "volume_ratio_low": 0.7 if provisional_last_bar else 0.55,
            "volume_ratio_high": 2.0 if provisional_last_bar else 1.8,
            "change_pct_low": 0.5 if provisional_last_bar else 0.0,
            "change_pct_high": 5.8,
            "main_inflow_pct": -2.5,
            "overhead_gap_pct": 2.4,
            "amplitude_pct": 6.8,
        }
    if normalized_route == "strict_stealth":
        return {
            "sector_strength": STEALTH_FIRST_BOARD_MIN_SECTOR_STRENGTH,
            "sector_continuity": STEALTH_FIRST_BOARD_MIN_SECTOR_CONTINUITY,
            "support_strength": STEALTH_FIRST_BOARD_MIN_SUPPORT_STRENGTH,
            "bull_score": STEALTH_FIRST_BOARD_MIN_BULL_SCORE,
            "kline_score": STEALTH_FIRST_BOARD_MIN_INTRADAY_KLINE_SCORE if provisional_last_bar else STEALTH_FIRST_BOARD_MIN_KLINE_SCORE,
            "strict_confirmation_count": 3.0,
            "volume_ratio_low": 0.8,
            "volume_ratio_high": 2.2,
            "change_pct_low": 1.0,
            "change_pct_high": 6.8,
            "main_inflow_pct": -1.5,
            "overhead_gap_pct": 2.8,
        }
    return {
        "sector_strength": STRICT_FIRST_BOARD_MIN_SECTOR_STRENGTH,
        "sector_continuity": STRICT_FIRST_BOARD_MIN_SECTOR_CONTINUITY,
        "support_strength": STRICT_FIRST_BOARD_MIN_SUPPORT_STRENGTH,
        "bull_score": STRICT_FIRST_BOARD_MIN_BULL_SCORE,
        "kline_score": STRICT_FIRST_BOARD_MIN_INTRADAY_KLINE_SCORE if provisional_last_bar else STRICT_FIRST_BOARD_MIN_KLINE_SCORE,
        "strict_confirmation_count": 2.0,
        "main_inflow_pct": 5.0,
        "breakthrough_volume_ratio": 1.3,
        "change_pct_high": 8.5,
    }


def _append_first_board_suggestion(suggestions: list[str], message: str) -> None:
    normalized = str(message or "").strip()
    if normalized and normalized not in suggestions:
        suggestions.append(normalized)


def _append_first_board_action(actions: list[dict], *, key: str, label: str, hint: str) -> None:
    normalized_key = str(key or "").strip()
    normalized_hint = str(hint or "").strip()
    if not normalized_key or not normalized_hint:
        return
    if any(str(item.get("key") or "") == normalized_key and str(item.get("hint") or "") == normalized_hint for item in actions):
        return
    actions.append({"key": normalized_key, "label": str(label or normalized_key), "hint": normalized_hint})


def _build_first_board_actionable_actions(entry: dict) -> list[dict]:
    blockers = [str(item or "").strip() for item in entry.get("blockers") or [] if str(item or "").strip()]
    primary_reason = str(entry.get("primary_reason") or "")
    route = str(entry.get("diagnostic_route") or "strict_hot")
    provisional_last_bar = bool(entry.get("provisional_last_bar"))
    thresholds = _first_board_diagnostic_thresholds(route, provisional_last_bar=provisional_last_bar)
    actions: list[dict] = []

    sector_strength = _safe_float(entry.get("sector_strength_score"))
    sector_continuity = _safe_float(entry.get("sector_continuity_score"))
    support_strength = _safe_float(entry.get("support_strength_score"))
    bull_score = _safe_float(entry.get("bull_score"))
    memory_score = _safe_float(entry.get("memory_score"))
    volume_ratio = _safe_float(entry.get("volume_ratio"))
    change_pct = _safe_float(entry.get("change_pct"))
    main_inflow_pct = _safe_float(entry.get("main_net_inflow_pct"))
    kline_score = _safe_float(entry.get("kline_confirmation_score"))
    strict_confirmation_count = _safe_int(entry.get("strict_confirmation_count"))
    overhead_gap_pct = _safe_float(entry.get("overhead_gap_pct"))
    amplitude = _safe_float(entry.get("amplitude"))

    ordered_blockers = [
        blocker
        for _index, blocker in sorted(
            enumerate(blockers),
            key=lambda item: (0 if _classify_first_board_blocker(item[1]) == primary_reason else 1, item[0]),
        )
    ]

    for blocker in ordered_blockers:
        if blocker == "主线强度不足":
            target = thresholds.get("sector_strength", 0.0)
            _append_first_board_action(
                actions,
                key="sector_strength",
                label="抬主线强度",
                hint=f"主线强度至少回到 {target:.0f}+，当前还差 {max(target - sector_strength, 0.0):.1f} 分",
            )
        elif blocker == "题材持续性不足":
            target = thresholds.get("sector_continuity", 0.0)
            _append_first_board_action(
                actions,
                key="sector_continuity",
                label="补题材连续性",
                hint=f"题材连续性至少到 {target:.2f}+，当前还差 {max(target - sector_continuity, 0.0):.2f}",
            )
        elif blocker == "牛股分不足":
            target = thresholds.get("bull_score", 0.0)
            _append_first_board_action(
                actions,
                key="bull_score",
                label="补牛股分",
                hint=f"牛股分至少到 {target:.0f}+，当前还差 {max(target - bull_score, 0.0):.1f} 分",
            )
        elif blocker == "承接分不足":
            target = thresholds.get("support_strength", 0.0)
            _append_first_board_action(
                actions,
                key="support_strength",
                label="抬承接分",
                hint=f"承接分至少到 {target:.0f}+，当前还差 {max(target - support_strength, 0.0):.1f} 分",
            )
        elif blocker == "资金流入不够强":
            target = thresholds.get("main_inflow_pct", 5.0)
            _append_first_board_action(
                actions,
                key="main_inflow_pct",
                label="补主力净流入",
                hint=f"主力净流入占比至少回到 {target:.1f}%+，当前还差 {max(target - main_inflow_pct, 0.0):.1f} 个点",
            )
        elif blocker == "突破量能不足":
            target = thresholds.get("breakthrough_volume_ratio", 1.3)
            _append_first_board_action(
                actions,
                key="breakthrough_volume_ratio",
                label="补突破量比",
                hint=f"突破量比至少到 {target:.1f}+，当前还差 {max(target - volume_ratio, 0.0):.2f}",
            )
        elif blocker == "热启动触发强度不足":
            min_inflow_target = thresholds.get("main_inflow_pct", FRESH_HOT_FIRST_BOARD_MIN_MAIN_INFLOW_PCT)
            min_volume_target = thresholds.get(
                "breakthrough_volume_ratio",
                FRESH_HOT_FIRST_BOARD_MIN_BREAKTHROUGH_VOLUME_RATIO,
            )
            _append_first_board_action(
                actions,
                key="fresh_hot_trigger",
                label="补热启动强度",
                hint=(
                    f"无记忆热启动至少要看到净流入占比 {min_inflow_target:.1f}%+ 或突破量比 {min_volume_target:.1f}+，"
                    f"当前净流入/量比分别是 {main_inflow_pct:.1f}% / {volume_ratio:.2f}"
                ),
            )
        elif blocker == "量能形态不符合静默蓄势":
            low = thresholds.get("volume_ratio_low")
            high = thresholds.get("volume_ratio_high")
            if low is not None and volume_ratio < low:
                _append_first_board_action(
                    actions,
                    key="volume_ratio_low",
                    label="补静默量比",
                    hint=f"量比至少回到 {low:.2f}+，当前还差 {max(low - volume_ratio, 0.0):.2f}",
                )
            elif high is not None and volume_ratio > high:
                _append_first_board_action(
                    actions,
                    key="volume_ratio_high",
                    label="压回静默量比区间",
                    hint=f"量比最好压回 {high:.2f} 内，当前高出 {max(volume_ratio - high, 0.0):.2f}",
                )
        elif blocker == "资金明显背离":
            target = thresholds.get("main_inflow_pct", -1.5)
            _append_first_board_action(
                actions,
                key="main_inflow_repair",
                label="修复资金背离",
                hint=f"主力净流入占比至少回到 {target:.1f}% 以上，当前还差 {max(target - main_inflow_pct, 0.0):.1f} 个点",
            )
        elif blocker == "未形成量窒息":
            _append_first_board_action(
                actions,
                key="volume_suffocation",
                label="形成量窒息",
                hint="先等平台量能继续缩到静默区，再看是否出现贴近前高的弱转强",
            )
        elif blocker == "缺少首波记忆或主线共振":
            memory_target = thresholds.get("memory_score", 28.0)
            _append_first_board_action(
                actions,
                key="memory_score",
                label="补首波记忆分",
                hint=f"首波记忆分至少到 {memory_target:.0f}+，当前还差 {max(memory_target - memory_score, 0.0):.1f} 分",
            )
            _append_first_board_action(
                actions,
                key="mainline_resonance",
                label="补主线共振三件套",
                hint=(
                    "如果记忆短期补不上，就让主线/承接/牛股至少同步回到 "
                    f"{thresholds.get('mainline_sector_strength', thresholds.get('sector_strength', 50.0)):.0f}/"
                    f"{thresholds.get('mainline_support_strength', thresholds.get('support_strength', 55.0)):.0f}/"
                    f"{thresholds.get('mainline_bull_score', thresholds.get('bull_score', 72.0)):.0f}+"
                ),
            )
        elif blocker in {"K线结构共振不足", "静默蓄势结构不足", "静默结构确认不足 4 项"}:
            target = thresholds.get("strict_confirmation_count", 2.0)
            _append_first_board_action(
                actions,
                key="strict_confirmation_count",
                label="补结构确认项",
                hint=f"结构确认项至少补到 {int(target)} 项，当前还差 {max(int(target) - strict_confirmation_count, 0)} 项",
            )
        elif blocker == "十字星/结构确认不够硬":
            _append_first_board_action(
                actions,
                key="qualified_doji",
                label="等确认K硬化",
                hint="优先等一根更像确认K的十字/缩量小阳线，把结构硬度补上再看",
            )
        elif blocker == "K线确认分不足":
            target = thresholds.get("kline_score", 0.52)
            _append_first_board_action(
                actions,
                key="kline_score",
                label="补K线确认分",
                hint=f"K线确认分至少到 {target:.2f}+，当前还差 {max(target - kline_score, 0.0):.2f}",
            )
        elif blocker == "距离前高仍偏远":
            target = thresholds.get("overhead_gap_pct", 2.8)
            _append_first_board_action(
                actions,
                key="overhead_gap_pct",
                label="收敛离前高距离",
                hint=f"离前高距离先收敛到 {target:.1f}% 内，当前还多 {max(overhead_gap_pct - target, 0.0):.1f} 个点",
            )
        elif blocker == "缺少缩量平台突破前形态":
            _append_first_board_action(
                actions,
                key="platform_shape",
                label="补平台突破前形态",
                hint="先等平台压缩、量窒息和贴近前高三件套同时成立，再考虑点火",
            )
        elif blocker == "振幅过大不符静默横盘":
            target = thresholds.get("amplitude_pct", 6.8)
            _append_first_board_action(
                actions,
                key="amplitude_pct",
                label="压缩振幅",
                hint=f"振幅先压回 {target:.1f}% 内，当前还高出 {max(amplitude - target, 0.0):.1f} 个点",
            )
        elif blocker == "涨幅区间不符合静默蓄势":
            low = thresholds.get("change_pct_low")
            high = thresholds.get("change_pct_high")
            if low is not None and change_pct < low:
                _append_first_board_action(
                    actions,
                    key="change_pct_low",
                    label="抬日内涨幅到点火区间",
                    hint=f"日内涨幅至少回到 {low:.1f}%+ 才更像临门一脚，当前还差 {max(low - change_pct, 0.0):.1f} 个点",
                )
            elif high is not None and change_pct >= high:
                _append_first_board_action(
                    actions,
                    key="change_pct_high",
                    label="压回日内涨幅区间",
                    hint=f"日内涨幅最好控制在 {high:.1f}% 以下，当前高出 {max(change_pct - high, 0.0):.1f} 个点",
                )
        elif blocker == "涨幅区间不符合热启动":
            low = thresholds.get("change_pct_low", FRESH_HOT_FIRST_BOARD_MIN_CHANGE_PCT)
            high = thresholds.get("change_pct_high", FRESH_HOT_FIRST_BOARD_MAX_CHANGE_PCT)
            if change_pct < low:
                _append_first_board_action(
                    actions,
                    key="change_pct_low",
                    label="抬热启动涨幅",
                    hint=f"日内涨幅至少回到 {low:.1f}%+ 才更像热启动，当前还差 {max(low - change_pct, 0.0):.1f} 个点",
                )
            elif change_pct >= high:
                _append_first_board_action(
                    actions,
                    key="change_pct_high",
                    label="压回热启动涨幅区间",
                    hint=f"热启动涨幅最好控制在 {high:.1f}% 以下，当前高出 {max(change_pct - high, 0.0):.1f} 个点",
                )
        elif blocker == "涨幅过大接近板上":
            target = thresholds.get("change_pct_high", 8.5)
            _append_first_board_action(
                actions,
                key="change_pct_hot_ceiling",
                label="避免涨幅过热",
                hint=f"日内涨幅最好别超过 {target:.1f}% 才更适合首板预判，当前高出 {max(change_pct - target, 0.0):.1f} 个点",
            )
        elif blocker == "上冲回落风险":
            _append_first_board_action(
                actions,
                key="failed_reversal",
                label="消化上冲回落风险",
                hint="先等上冲回落次数归零，再看是否能留下更干净的收盘结构",
            )
        elif blocker == "K线历史不足":
            _append_first_board_action(
                actions,
                key="kline_history",
                label="补足K线历史",
                hint="先补足最近一段K线历史，至少让平台周期和结构确认能算得更完整",
            )
        elif blocker == "缺少显性点火或静默蓄势触发":
            _append_first_board_action(
                actions,
                key="effective_trigger",
                label="等有效触发",
                hint="先出现资金点火、突破新高或静默蓄势触发，再谈首板入池",
            )

    if not actions:
        _append_first_board_action(
            actions,
            key="fallback",
            label="补最前置 blocker",
            hint="先补最靠前的 blocker，再回头看主线、量能和结构三件套",
        )
    return actions[:3]


def _build_first_board_actionable_suggestions(entry: dict) -> list[str]:
    return [str(item.get("hint") or "") for item in _build_first_board_actionable_actions(entry) if str(item.get("hint") or "").strip()]


def _resolve_first_board_block_reason(blockers: list[str]) -> tuple[str, list[str]]:
    categories = list(
        dict.fromkeys(
            _classify_first_board_blocker(blocker)
            for blocker in blockers
            if str(blocker or "").strip()
        )
    )
    for reason in FIRST_BOARD_DIAGNOSTIC_REASON_ORDER:
        if reason in categories:
            return reason, [item for item in FIRST_BOARD_DIAGNOSTIC_REASON_ORDER if item in categories]
    return "其他约束", ["其他约束"]


def _first_board_diagnostic_relevance(entry: dict) -> float:
    return (
        _safe_float(entry.get("bull_score"))
        + _safe_float(entry.get("sector_strength_score")) * 0.6
        + _safe_float(entry.get("support_strength_score")) * 0.5
        + _safe_float(entry.get("memory_score")) * 0.5
        + max(_safe_float(entry.get("change_pct")), 0.0) * 2.0
        + max(_safe_float(entry.get("volume_ratio")) - 1.0, 0.0) * 8.0
    )


def _build_first_board_diagnostics_overview(
    diagnostics: dict[str, dict],
    reason_summary: list[dict],
) -> dict:
    total = len(diagnostics)
    if total <= 0 or not reason_summary:
        return {
            "headline": "当前没有足够的未入池样本，暂时看不出今天首板最缺什么。",
            "market_takeaway": "",
            "dominant_reason": "",
            "dominant_reason_label": "",
            "dominant_count": 0,
            "dominant_share": 0.0,
            "secondary_reason": "",
            "secondary_reason_label": "",
            "secondary_count": 0,
            "secondary_share": 0.0,
            "top_focuses": [],
        }

    dominant = reason_summary[0]
    dominant_reason = str(dominant.get("reason") or "")
    dominant_label = str(dominant.get("label") or dominant_reason)
    dominant_count = _safe_int(dominant.get("count"))
    dominant_share = dominant_count / total if total else 0.0

    secondary = reason_summary[1] if len(reason_summary) > 1 else None
    secondary_reason = str((secondary or {}).get("reason") or "")
    secondary_label = str((secondary or {}).get("label") or secondary_reason)
    secondary_count = _safe_int((secondary or {}).get("count"))
    secondary_share = secondary_count / total if total else 0.0

    if dominant_share >= 0.55:
        headline = (
            f"今日首板主要死在“{dominant_label}”，{dominant_count} / {total} 只未入池样本首先卡在这里"
            f"（{dominant_share * 100:.1f}%）。"
        )
    elif secondary_count > 0:
        headline = (
            f"今日首板最缺的是“{dominant_label}”，但“{secondary_label}”也很集中，"
            f"两者合计占了 {(dominant_share + secondary_share) * 100:.1f}% 的未入池样本。"
        )
    else:
        headline = (
            f"今日首板最缺的是“{dominant_label}”，占全部未入池样本的 {dominant_share * 100:.1f}%。"
        )

    focus_map: dict[str, dict] = {}
    for entry in diagnostics.values():
        key = str(entry.get("next_focus_key") or "").strip()
        label = str(entry.get("next_focus_label") or "").strip()
        hint = str(entry.get("next_threshold_hint") or "").strip()
        if not key:
            continue
        bucket = focus_map.setdefault(
            key,
            {
                "key": key,
                "label": label or key,
                "count": 0,
                "sample_hint": hint,
                "reason": str(entry.get("primary_reason") or ""),
            },
        )
        bucket["count"] = _safe_int(bucket.get("count")) + 1
        if not str(bucket.get("sample_hint") or "").strip() and hint:
            bucket["sample_hint"] = hint

    top_focuses = sorted(
        focus_map.values(),
        key=lambda item: (_safe_int(item.get("count")), str(item.get("label") or "")),
        reverse=True,
    )[:3]
    for item in top_focuses:
        count = _safe_int(item.get("count"))
        item["share"] = round(count / total, 4) if total else 0.0

    return {
        "headline": headline,
        "market_takeaway": FIRST_BOARD_DIAGNOSTIC_REASON_TAKEAWAYS.get(dominant_reason, ""),
        "dominant_reason": dominant_reason,
        "dominant_reason_label": dominant_label,
        "dominant_count": dominant_count,
        "dominant_share": round(dominant_share, 4),
        "secondary_reason": secondary_reason,
        "secondary_reason_label": secondary_label,
        "secondary_count": secondary_count,
        "secondary_share": round(secondary_share, 4),
        "top_focuses": top_focuses,
    }


def _record_first_board_diagnostic(
    diagnostics: dict[str, dict],
    *,
    code: str,
    name: str,
    blockers: list[str],
    signal_summary: str = "",
    trigger_reason: str = "",
    sector_name: str = "",
    latest_as_of: str = "",
    change_pct: float = 0.0,
    volume_ratio: float = 0.0,
    support_strength_score: float = 0.0,
    sector_strength_score: float = 0.0,
    sector_continuity_score: float = 0.0,
    memory_score: float = 0.0,
    bull_score: float = 0.0,
    main_net_inflow_pct: float = 0.0,
    kline_confirmation_score: float = 0.0,
    strict_confirmation_count: int = 0,
    overhead_gap_pct: float = 0.0,
    amplitude: float = 0.0,
    provisional_last_bar: bool = False,
    diagnostic_route: str = "",
) -> None:
    normalized_code = str(code or "").strip()
    normalized_name = str(name or "").strip()
    unique_blockers = list(dict.fromkeys(str(item or "").strip() for item in blockers if str(item or "").strip()))
    if not normalized_code or not unique_blockers:
        return

    primary_reason, reason_categories = _resolve_first_board_block_reason(unique_blockers)
    entry = {
        "code": normalized_code,
        "name": normalized_name,
        "primary_reason": primary_reason,
        "reason_categories": reason_categories,
        "blockers": unique_blockers,
        "signal_summary": str(signal_summary or "").strip(),
        "trigger_reason": str(trigger_reason or "").strip(),
        "sector_name": str(sector_name or "").strip(),
        "latest_as_of": str(latest_as_of or "").strip(),
        "change_pct": round(_safe_float(change_pct), 2),
        "volume_ratio": round(_safe_float(volume_ratio), 2),
        "support_strength_score": round(_safe_float(support_strength_score), 1),
        "sector_strength_score": round(_safe_float(sector_strength_score), 1),
        "sector_continuity_score": round(_safe_float(sector_continuity_score), 3),
        "memory_score": round(_safe_float(memory_score), 1),
        "bull_score": round(_safe_float(bull_score), 1),
        "main_net_inflow_pct": round(_safe_float(main_net_inflow_pct), 2),
        "kline_confirmation_score": round(_safe_float(kline_confirmation_score), 3),
        "strict_confirmation_count": _safe_int(strict_confirmation_count),
        "overhead_gap_pct": round(_safe_float(overhead_gap_pct), 2),
        "amplitude": round(_safe_float(amplitude), 2),
        "provisional_last_bar": bool(provisional_last_bar),
        "diagnostic_route": str(diagnostic_route or "strict_hot"),
    }
    entry["actionable_actions"] = _build_first_board_actionable_actions(entry)
    entry["actionable_suggestions"] = [str(item.get("hint") or "") for item in entry["actionable_actions"]]
    entry["next_focus_key"] = str((entry["actionable_actions"][0] if entry["actionable_actions"] else {}).get("key") or "")
    entry["next_focus_label"] = str((entry["actionable_actions"][0] if entry["actionable_actions"] else {}).get("label") or "")
    entry["next_threshold_hint"] = entry["actionable_suggestions"][0] if entry["actionable_suggestions"] else ""
    current = diagnostics.get(normalized_code)
    if current is None:
        diagnostics[normalized_code] = entry
        return

    merged_blockers = list(dict.fromkeys((current.get("blockers") or []) + unique_blockers))
    merged_reason, merged_categories = _resolve_first_board_block_reason(merged_blockers)
    preferred = entry if _first_board_diagnostic_relevance(entry) >= _first_board_diagnostic_relevance(current) else current
    diagnostics[normalized_code] = {
        **current,
        **preferred,
        "primary_reason": merged_reason,
        "reason_categories": merged_categories,
        "blockers": merged_blockers,
    }
    diagnostics[normalized_code]["actionable_actions"] = _build_first_board_actionable_actions(diagnostics[normalized_code])
    diagnostics[normalized_code]["actionable_suggestions"] = [
        str(item.get("hint") or "") for item in diagnostics[normalized_code]["actionable_actions"]
    ]
    diagnostics[normalized_code]["next_focus_key"] = str(
        (diagnostics[normalized_code]["actionable_actions"][0] if diagnostics[normalized_code]["actionable_actions"] else {}).get("key") or ""
    )
    diagnostics[normalized_code]["next_focus_label"] = str(
        (diagnostics[normalized_code]["actionable_actions"][0] if diagnostics[normalized_code]["actionable_actions"] else {}).get("label") or ""
    )
    diagnostics[normalized_code]["next_threshold_hint"] = (
        diagnostics[normalized_code]["actionable_suggestions"][0]
        if diagnostics[normalized_code]["actionable_suggestions"]
        else ""
    )


def _build_first_board_diagnostics_payload(
    diagnostics: dict[str, dict],
    *,
    limit_per_reason: int = 6,
) -> dict:
    if not diagnostics:
        return {
            "blocked_total": 0,
            "reason_summary": [],
            "blocked_reason_groups": [],
            "notes": ["当前没有记录到首板主筛选挡掉的样本"],
        }

    groups: dict[str, list[dict]] = {reason: [] for reason in FIRST_BOARD_DIAGNOSTIC_REASON_ORDER}
    for entry in diagnostics.values():
        groups.setdefault(str(entry.get("primary_reason") or "其他约束"), []).append(entry)

    reason_summary: list[dict] = []
    blocked_reason_groups: list[dict] = []
    for reason in FIRST_BOARD_DIAGNOSTIC_REASON_ORDER:
        items = groups.get(reason) or []
        if not items:
            continue
        items.sort(
            key=lambda item: (
                _safe_float(item.get("memory_score")),
                _safe_float(item.get("bull_score")),
                _safe_float(item.get("sector_strength_score")),
                _safe_float(item.get("support_strength_score")),
                _safe_float(item.get("volume_ratio")),
                _safe_float(item.get("change_pct")),
            ),
            reverse=True,
        )
        reason_summary.append({"reason": reason, "label": reason, "count": len(items)})
        blocked_reason_groups.append(
            {
                "reason": reason,
                "label": reason,
                "count": len(items),
                "playbook": list(FIRST_BOARD_DIAGNOSTIC_PLAYBOOKS.get(reason, ())),
                "examples": items[:limit_per_reason],
            }
        )

    # 复盘首页的“主失败原因”必须由真实样本量决定；枚举顺序只用于同数
    # 量时稳定排序，否则会把小样本门槛误报成当天主要漏选根因。
    reason_order = {
        reason: index for index, reason in enumerate(FIRST_BOARD_DIAGNOSTIC_REASON_ORDER)
    }
    reason_summary.sort(
        key=lambda item: (
            -_safe_int(item.get("count")),
            reason_order.get(str(item.get("reason") or ""), len(reason_order)),
        )
    )
    blocked_reason_groups.sort(
        key=lambda item: (
            -_safe_int(item.get("count")),
            reason_order.get(str(item.get("reason") or ""), len(reason_order)),
        )
    )

    return {
        "blocked_total": len(diagnostics),
        "overview": _build_first_board_diagnostics_overview(diagnostics, reason_summary),
        "reason_summary": reason_summary,
        "blocked_reason_groups": blocked_reason_groups,
        "notes": [
            "诊断样本已剔除跌停、冲高回落等负向异动，避免把高位退潮票误当成首板失败样本",
            "按主因分组展示；单只股票只归到一个主因，完整拦截项保留在明细中",
            "每只样例会给出最先该补的阈值提示，方便直接判断差的是主线、量能还是结构",
        ],
    }


async def _get_latest_limit_up_trade_date(db: AsyncSession) -> date:
    today = date.today()
    candidates = (
        await db.execute(
            select(LimitUpPool.trade_date)
            .where(
                LimitUpPool.trade_date <= today,
                LimitUpPool.quarantined.is_(False),
            )
            .distinct()
            .order_by(desc(LimitUpPool.trade_date))
            .limit(20)
        )
    ).scalars().all()
    for candidate in candidates:
        if candidate and await trade_calendar.is_trade_day(candidate):
            return candidate
    latest_any = await db.scalar(
        select(func.max(LimitUpPool.trade_date)).where(
            LimitUpPool.quarantined.is_(False)
        )
    )
    return latest_any or today


async def _resolve_second_board_source_trade_date(
    db: AsyncSession,
    *,
    snapshot_source: str,
    snapshot_context: str,
    recorded_at: datetime,
) -> date:
    """Resolve the first-board pool used to predict a second board.

    A morning official batch is consumed in the current session, but its T2
    evidence must remain anchored to the previous completed trading day. Live
    current-day limit-up rows can already exist at 09:35 and must never replace
    that closed pool.
    """

    normalized_source = _normalize_prediction_snapshot_source(snapshot_source)
    normalized_context = _normalize_prediction_snapshot_context(
        snapshot_context,
        source=normalized_source,
    )
    if (
        normalized_source == "schedule"
        and normalized_context in PROMOTION_INTRADAY_CONTEXTS
    ):
        return await trade_calendar.previous_trade_day(recorded_at.date())
    return await _get_latest_limit_up_trade_date(db)


def _prediction_trade_dates_by_target(
    *,
    first_board_source_trade_date: date,
    second_board_source_trade_date: date,
    snapshot_source: str,
    snapshot_context: str,
    recorded_at: datetime,
) -> dict[int, date]:
    """Separate immutable signal-session dates from candidate evidence dates."""

    normalized_source = _normalize_prediction_snapshot_source(snapshot_source)
    normalized_context = _normalize_prediction_snapshot_context(
        snapshot_context,
        source=normalized_source,
    )
    if (
        normalized_source == "schedule"
        and normalized_context in PROMOTION_INTRADAY_CONTEXTS
    ):
        signal_session_date = recorded_at.date()
        return {1: signal_session_date, 2: signal_session_date}
    return {
        1: first_board_source_trade_date,
        2: second_board_source_trade_date,
    }


async def _get_previous_limit_up_trade_date(db: AsyncSession, trade_date: date) -> date | None:
    result = await db.execute(
        select(func.max(LimitUpPool.trade_date)).where(
            LimitUpPool.trade_date < trade_date,
            LimitUpPool.quarantined.is_(False),
        )
    )
    return result.scalar_one_or_none()


async def _load_limit_up_rows(db: AsyncSession, trade_date: date) -> list[LimitUpPool]:
    result = await db.execute(
        select(LimitUpPool)
        .where(
            LimitUpPool.trade_date == trade_date,
            LimitUpPool.quarantined.is_(False),
        )
        .order_by(
            desc(LimitUpPool.consecutive_days),
            desc(LimitUpPool.seal_amount),
            LimitUpPool.code,
        )
    )
    return result.scalars().all()


async def _normalize_limit_up_consecutive_days(
    db: AsyncSession,
    trade_date: date,
    rows: list[LimitUpPool],
) -> dict[str, int]:
    codes = [str(row.code or "").strip() for row in rows if str(row.code or "").strip()]
    if not codes:
        return {}

    date_rows = (
        await db.execute(
            select(LimitUpPool.trade_date)
            .where(LimitUpPool.trade_date <= trade_date)
            .distinct()
            .order_by(desc(LimitUpPool.trade_date))
            .limit(12)
        )
    ).all()
    trade_dates = sorted(row[0] for row in date_rows if row[0] is not None)
    if not trade_dates:
        return {}

    history_rows = (
        await db.execute(
            select(
                LimitUpPool.code,
                LimitUpPool.trade_date,
                LimitUpPool.consecutive_days,
            ).where(
                LimitUpPool.code.in_(codes),
                LimitUpPool.trade_date.in_(trade_dates),
            )
        )
    ).all()
    by_date: dict[date, dict[str, int]] = defaultdict(dict)
    for code, row_trade_date, consecutive_days in history_rows:
        normalized_code = str(code or "").strip()
        if not normalized_code or row_trade_date is None:
            continue
        by_date[row_trade_date][normalized_code] = max(_safe_int(consecutive_days, 1), 1)

    streaks: dict[str, int] = {}
    normalized_current: dict[str, int] = {}
    for row_trade_date in trade_dates:
        current_day_codes = by_date.get(row_trade_date, {})
        next_streaks: dict[str, int] = {}
        for code, stored_days in current_day_codes.items():
            derived_days = max(stored_days, streaks.get(code, 0) + 1 if code in streaks else 1)
            next_streaks[code] = derived_days
            if row_trade_date == trade_date:
                normalized_current[code] = derived_days
        streaks = next_streaks
    return normalized_current


async def _load_filtered_limit_ups(db: AsyncSession, trade_date: date | None) -> list[dict]:
    if trade_date is None:
        return []

    rows = await _load_limit_up_rows(db, trade_date)
    normalized_days = await _normalize_limit_up_consecutive_days(db, trade_date, rows)
    raw_items = [
        {
            "code": row.code,
            "name": row.name or "",
            "seal_amount": float(row.seal_amount or 0),
            "break_count": int(row.break_count or 0),
            "turnover": float(row.turnover or 0),
            "limit_up_time": row.limit_up_time or "",
            "consecutive_days": normalized_days.get(str(row.code or "").strip(), int(row.consecutive_days or 1)),
            "trade_date": str(row.trade_date),
            "source": row.source or "",
            "limit_up_reason": row.limit_up_reason or "",
        }
        for row in rows
    ]
    return await stock_tagger.filter_signals(db, raw_items)


async def _load_sentiment_cycle(db: AsyncSession, trade_date: date) -> str:
    result = await db.execute(
        select(MarketSentiment.sentiment_cycle)
        .where(MarketSentiment.trade_date == trade_date)
        .limit(1)
    )
    sentiment = result.scalar_one_or_none()
    return _normalize_promotion_sentiment(sentiment)


async def _load_sector_rotation_context(
    db: AsyncSession,
    trade_date: date,
    sector_codes: list[str],
) -> dict[str, dict]:
    normalized_codes = list(dict.fromkeys(str(code or "").strip() for code in sector_codes if str(code or "").strip()))
    if not normalized_codes:
        return {}

    start_date = trade_date - timedelta(days=8)
    result = await db.execute(
        select(
            SectorPersistence.sector_code,
            SectorPersistence.trade_date,
            SectorPersistence.strength_score,
            SectorPersistence.limit_up_count,
            SectorPersistence.fund_flow,
            SectorPersistence.change_pct,
            SectorPersistence.consecutive_days,
            SectorLifecycle.limit_up_count,
            SectorLifecycle.lifecycle_state,
            SectorLifecycle.active_days,
        ).outerjoin(
            SectorLifecycle,
            and_(
                SectorLifecycle.sector_code == SectorPersistence.sector_code,
                SectorLifecycle.trade_date == SectorPersistence.trade_date,
            ),
        ).where(
            SectorPersistence.sector_code.in_(normalized_codes),
            SectorPersistence.trade_date >= start_date,
            SectorPersistence.trade_date <= trade_date,
        )
    )

    grouped: dict[str, list[dict]] = {}
    for row in result.all():
        code = str(row[0] or "").strip()
        if not code:
            continue
        lifecycle_verified = row[7] is not None
        raw_limit_up_count = _safe_int(row[3])
        attributed_limit_up_count = _safe_int(row[7]) if lifecycle_verified else raw_limit_up_count
        strength_score = _safe_float(row[2])
        fund_flow = _safe_float(row[4])
        change_pct = _safe_float(row[5])
        # 泛概念成员数会把不相关涨停重复计入。若生命周期归因宽度明显小于
        # 原始映射，且板块价/资同时为负，强度必须降级而不是继续冒充主线。
        attribution_mismatch = bool(
            lifecycle_verified
            and raw_limit_up_count >= max(attributed_limit_up_count * 3, 5)
        )
        if attribution_mismatch and change_pct <= 0 and fund_flow <= 0:
            strength_score = min(strength_score, 32.0)
        grouped.setdefault(code, []).append(
            {
                "trade_date": row[1],
                "strength_score": strength_score,
                "limit_up_count": attributed_limit_up_count,
                "raw_limit_up_count": raw_limit_up_count,
                "fund_flow": fund_flow,
                "change_pct": change_pct,
                "consecutive_days": _safe_int(row[9]) if lifecycle_verified else _safe_int(row[6]),
                "lifecycle_state": str(row[8] or ""),
                "attribution_verified": lifecycle_verified,
                "attribution_mismatch": attribution_mismatch,
            }
        )

    raw_context: dict[str, dict] = {}
    for code, rows in grouped.items():
        current_rows = [row for row in rows if row.get("trade_date") == trade_date]
        if not current_rows:
            continue
        current = max(
            current_rows,
            key=lambda row: (
                _safe_float(row.get("strength_score")),
                _safe_int(row.get("limit_up_count")),
                _safe_float(row.get("fund_flow")),
            ),
        )
        previous_rows = sorted(
            [row for row in rows if row.get("trade_date") and row.get("trade_date") < trade_date],
            key=lambda row: row.get("trade_date"),
        )
        recent_previous_rows = previous_rows[-6:]
        previous_strengths = [_safe_float(row.get("strength_score")) for row in previous_rows]
        previous_limit_ups = [_safe_int(row.get("limit_up_count")) for row in previous_rows]
        previous_fund_flows = [_safe_float(row.get("fund_flow")) for row in previous_rows]

        current_strength = _safe_float(current.get("strength_score"))
        current_limit_ups = _safe_int(current.get("limit_up_count"))
        current_fund_flow = _safe_float(current.get("fund_flow"))
        current_change_pct = _safe_float(current.get("change_pct"))
        current_consecutive_days = _safe_int(current.get("consecutive_days"))
        previous_avg_strength = _mean(previous_strengths)
        previous_max_strength = max(previous_strengths) if previous_strengths else 0.0
        previous_avg_limit_ups = _mean(previous_limit_ups)
        previous_avg_fund_flow = _mean(previous_fund_flows)
        strength_delta = current_strength - previous_avg_strength if previous_rows else current_strength
        limit_up_delta = current_limit_ups - previous_avg_limit_ups if previous_rows else float(current_limit_ups)
        fund_flow_delta = current_fund_flow - previous_avg_fund_flow if previous_rows else current_fund_flow
        has_rotation_history = len(previous_rows) >= 2
        has_group_confirmation = current_limit_ups >= 2 or (
            current_limit_ups == 1
            and current_strength >= 48.0
            and strength_delta >= 18.0
            and fund_flow_delta >= 2.0
        )

        recent_active_days = sum(
            1
            for row in recent_previous_rows
            if _safe_float(row.get("strength_score")) >= 20.0
            or _safe_int(row.get("limit_up_count")) >= 2
        )
        recent_limit_up_sum = sum(_safe_int(row.get("limit_up_count")) for row in recent_previous_rows)
        recent_peak_limit_ups = max(
            (_safe_int(row.get("limit_up_count")) for row in recent_previous_rows),
            default=0,
        )
        retained_leader_ratio = (
            current_limit_ups / recent_peak_limit_ups
            if recent_peak_limit_ups > 0
            else 0.0
        )
        washout_score = _clamp_score(
            previous_max_strength * 0.25
            + min(current_limit_ups, 6) * 5.0
            + min(recent_active_days, 6) * 4.0
            + min(current_consecutive_days, 10) * 1.5
            + min(abs(min(current_change_pct, 0.0)), 6.0) * 3.0
            + min(retained_leader_ratio, 1.0) * 10.0
            - max(current_strength - 25.0, 0.0) * 0.20
        )
        # 近期持续活跃的主线在单日大跌后仍保留至少两只涨停火种，属于
        # “板块洗盘反转观察”，不能和连续数日衰退的旧题材一起直接剔除。
        # 这里只生成次日观察种子；真正升格仍必须等待竞价宽度或盘中量价确认。
        washout_reversal_watch = bool(
            len(previous_rows) >= 3
            and current_consecutive_days >= SECTOR_WASHOUT_WATCH_MIN_CONSECUTIVE_DAYS
            and SECTOR_WASHOUT_WATCH_MIN_CHANGE_PCT <= current_change_pct <= SECTOR_WASHOUT_WATCH_MAX_CHANGE_PCT
            and current_limit_ups >= SECTOR_WASHOUT_WATCH_MIN_CURRENT_LIMIT_UPS
            and previous_max_strength >= SECTOR_WASHOUT_WATCH_MIN_RECENT_PEAK_STRENGTH
            and recent_active_days >= SECTOR_WASHOUT_WATCH_MIN_RECENT_ACTIVE_DAYS
            and recent_limit_up_sum >= SECTOR_WASHOUT_WATCH_MIN_RECENT_LIMIT_UP_SUM
            and retained_leader_ratio >= 0.25
            and strength_delta <= -8.0
            and washout_score >= 55.0
        )

        low_position_rotation = bool(
            has_rotation_history
            and current_strength >= 36.0
            and current_limit_ups >= 1
            and has_group_confirmation
            and (
                strength_delta >= 10.0
                or limit_up_delta >= 2.0
                or (current_limit_ups >= 3 and previous_avg_limit_ups <= 1.2)
            )
            and previous_max_strength <= max(72.0, current_strength + 18.0)
            and current_consecutive_days <= 3
        )
        crowded_stale_theme = bool(
            not washout_reversal_watch
            and previous_max_strength >= 72.0
            and previous_rows
            and strength_delta <= -7.0
            and current_limit_ups <= max(1.0, previous_avg_limit_ups - 1.0)
        )
        if washout_reversal_watch:
            label = "活跃主线急跌洗盘"
        elif low_position_rotation:
            label = "低位轮动抬升"
        elif crowded_stale_theme:
            label = "高位拥挤退潮"
        elif strength_delta >= 8.0:
            label = "板块热度升温"
        elif strength_delta <= -6.0:
            label = "板块热度降温"
        else:
            label = "板块延续"

        raw_context[code] = {
            "sector_prev_avg_strength": round(previous_avg_strength, 2),
            "sector_prev_max_strength": round(previous_max_strength, 2),
            "sector_strength_delta": round(strength_delta, 2),
            "sector_limit_up_delta": round(limit_up_delta, 2),
            "sector_fund_flow_delta": round(fund_flow_delta, 2),
            "sector_current_strength": round(current_strength, 2),
            "sector_current_limit_up_count": current_limit_ups,
            "sector_current_fund_flow": round(current_fund_flow, 2),
            "sector_consecutive_days": current_consecutive_days,
            "sector_low_position_rotation": low_position_rotation,
            "sector_washout_reversal_watch": washout_reversal_watch,
            "sector_washout_score": round(washout_score, 2),
            "sector_recent_active_days": recent_active_days,
            "sector_recent_limit_up_sum": recent_limit_up_sum,
            "sector_recent_peak_limit_ups": recent_peak_limit_ups,
            "sector_retained_leader_ratio": round(retained_leader_ratio, 3),
            "sector_crowded_stale_theme": crowded_stale_theme,
            "sector_rotation_label": label,
        }

    # 资金流、涨停家数和强度差的原始量纲不同。旧实现直接把资金流差乘权重，
    # 在百亿元级板块资金下会让绝大多数候选被截断到100分，失去排序能力。
    # 改成同日全板块横截面百分位，并保留绝对强度和退潮惩罚。
    strength_delta_rank = _percentile_rank_map({
        code: _safe_float(item.get("sector_strength_delta")) for code, item in raw_context.items()
    })
    limit_up_delta_rank = _percentile_rank_map({
        code: _safe_float(item.get("sector_limit_up_delta")) for code, item in raw_context.items()
    })
    fund_flow_delta_rank = _percentile_rank_map({
        code: _safe_float(item.get("sector_fund_flow_delta")) for code, item in raw_context.items()
    })
    breadth_rank = _percentile_rank_map({
        code: _safe_float(item.get("sector_current_limit_up_count")) for code, item in raw_context.items()
    })

    context: dict[str, dict] = {}
    for code, item in raw_context.items():
        current_strength = _safe_float(item.get("sector_current_strength"))
        current_fund_flow = _safe_float(item.get("sector_current_fund_flow"))
        current_consecutive_days = _safe_int(item.get("sector_consecutive_days"))
        strength_delta = _safe_float(item.get("sector_strength_delta"))
        low_position_rotation = bool(item.get("sector_low_position_rotation"))
        crowded_stale_theme = bool(item.get("sector_crowded_stale_theme"))
        positive_flow_persistence = (
            8.0
            if current_fund_flow > 0 and current_consecutive_days >= 2
            else 4.0
            if current_fund_flow > 0
            else 0.0
        )
        rotation_score = _clamp_score(
            current_strength * 0.30
            + _safe_float(strength_delta_rank.get(code)) * 0.20
            + _safe_float(limit_up_delta_rank.get(code)) * 0.18
            + _safe_float(fund_flow_delta_rank.get(code)) * 0.16
            + _safe_float(breadth_rank.get(code)) * 0.10
            + positive_flow_persistence
            + (5.0 if low_position_rotation else 0.0)
            - (18.0 if crowded_stale_theme else 0.0)
        )
        if not low_position_rotation and strength_delta < 0:
            rotation_score = min(rotation_score, 48.0)
        context[code] = {
            **item,
            "sector_strength_delta_percentile": _safe_float(strength_delta_rank.get(code)),
            "sector_limit_up_delta_percentile": _safe_float(limit_up_delta_rank.get(code)),
            "sector_fund_flow_delta_percentile": _safe_float(fund_flow_delta_rank.get(code)),
            "sector_breadth_percentile": _safe_float(breadth_rank.get(code)),
            "sector_flow_persistence_score": positive_flow_persistence,
            "sector_rotation_score": round(rotation_score, 2),
        }
    return context


def _sector_reason_relevance(reason: str, sector_name: str) -> int:
    """限制“找到强板块但与个股涨停归因无关”的错配。"""
    normalized_reason = re.sub(r"[\s概念行业板块Ⅰ-ⅫⅠ-Ⅹ]", "", str(reason or ""))
    normalized_sector = re.sub(r"[\s概念行业板块Ⅰ-ⅫⅠ-Ⅹ]", "", str(sector_name or ""))
    if not normalized_reason or not normalized_sector:
        return 0
    if normalized_reason == normalized_sector:
        return 4
    if normalized_reason in normalized_sector or normalized_sector in normalized_reason:
        return 3
    reason_tokens = {
        token for token in re.split(r"[+/、，,；;\-]", normalized_reason) if len(token) >= 2
    }
    sector_tokens = {
        token for token in re.split(r"[+/、，,；;\-]", normalized_sector) if len(token) >= 2
    }
    if reason_tokens & sector_tokens:
        return 2
    if any(
        reason_token in sector_token or sector_token in reason_token
        for reason_token in reason_tokens
        for sector_token in sector_tokens
    ):
        return 2
    # 只补充经过审查的同一产业语义族，不做通用模糊匹配。例如“算力服务”
    # 应能归到“算力租赁”，但不能因此归到“数字水印”等共同持股概念。
    normalized_reason_lower = normalized_reason.lower()
    normalized_sector_lower = normalized_sector.lower()
    for family in THEME_REASON_FAMILIES:
        family_tokens = [str(token or "").lower() for token in family if token]
        if any(token in normalized_reason_lower for token in family_tokens) and any(
            token in normalized_sector_lower for token in family_tokens
        ):
            return 2
    return 0


def _is_attributed_live_sector_member(
    sector_name: str,
    sector_type: str,
    limit_up_reason: str,
) -> bool:
    """行业映射可直接确认；概念必须与涨停归因相符，避免泛概念虚假宽度。"""
    if str(sector_type or "").strip() in {"industry", "sw_l1", "sw_l2", "sw_l3"}:
        return True
    return _sector_reason_relevance(limit_up_reason, sector_name) >= 2


async def _load_live_main_board_sector_limit_up_context(
    db: AsyncSession,
    trade_date: date,
    sector_codes: list[str] | None = None,
) -> dict[str, dict]:
    """直接按当日涨停池计算主板板块宽度，避免板块派生任务慢一轮。

    这里只把已封板且可交易的沪深主板成员作为宽度证据；概念映射仍沿用
    已治理的板块表，并排除关系型伪板块。该结果只补充板块上下文，不绕过
    个股量价、位置和风控闸门。
    """
    conditions = [
        LimitUpPool.trade_date == trade_date,
        StockSectorMapping.sector_type.in_(ALLOWED_MAINLINE_SECTOR_TYPES),
    ]
    if sector_codes:
        conditions.append(StockSectorMapping.sector_code.in_(sector_codes))
    result = await db.execute(
        select(
            StockSectorMapping.sector_code,
            StockSectorMapping.sector_name,
            StockSectorMapping.sector_type,
            LimitUpPool.code,
            LimitUpPool.name,
            LimitUpPool.limit_up_reason,
            SectorInfo.is_excluded,
        )
        .join(LimitUpPool, LimitUpPool.code == StockSectorMapping.code)
        .outerjoin(SectorInfo, StockSectorMapping.sector_code == SectorInfo.sector_code)
        .where(*conditions)
    )
    context: dict[str, dict] = {}
    for sector_code, sector_name, sector_type, code, name, limit_up_reason, is_excluded in result.all():
        normalized_sector_code = str(sector_code or "").strip()
        normalized_sector_name = str(sector_name or "").strip()
        normalized_code = str(code or "").strip()
        if (
            not normalized_sector_code
            or not normalized_sector_name
            or not normalized_code
            or bool(_safe_int(is_excluded))
            or normalized_sector_name in GENERIC_RELATIONSHIP_SECTOR_NAMES
            or not stock_tagger.is_tradeable(normalized_code)
            or "ST" in str(name or "").upper().replace(" ", "")
            or not _is_attributed_live_sector_member(
                normalized_sector_name,
                str(sector_type or ""),
                str(limit_up_reason or ""),
            )
        ):
            continue
        bucket = context.setdefault(
            normalized_sector_code,
            {
                "sector_code": normalized_sector_code,
                "sector_name": normalized_sector_name,
                "sector_types": set(),
                "member_codes": set(),
            },
        )
        bucket["sector_types"].add(str(sector_type or ""))
        bucket["member_codes"].add(normalized_code)

    return {
        sector_code: {
            **item,
            "sector_types": sorted(item["sector_types"]),
            "member_codes": sorted(item["member_codes"]),
            "limit_up_count": len(item["member_codes"]),
        }
        for sector_code, item in context.items()
    }


async def _load_best_sector_context(
    db: AsyncSession,
    trade_date: date,
    codes: list[str],
    reason_by_code: dict[str, str] | None = None,
    use_live_limit_up_context: bool | None = None,
    allowed_sector_types: set[str] | None = None,
) -> dict[str, dict]:
    if not codes:
        return {}

    effective_sector_types = allowed_sector_types or ALLOWED_MAINLINE_SECTOR_TYPES
    mapping_result = await db.execute(
        select(
            StockSectorMapping.code,
            StockSectorMapping.sector_code,
            StockSectorMapping.sector_name,
            StockSectorMapping.sector_type,
            SectorInfo.is_excluded,
            SectorInfo.stock_count,
        )
        .outerjoin(SectorInfo, StockSectorMapping.sector_code == SectorInfo.sector_code)
        .where(
            StockSectorMapping.code.in_(codes),
            StockSectorMapping.sector_type.in_(effective_sector_types),
        )
    )
    mappings = mapping_result.all()
    filtered_mappings = [
        row for row in mappings
        if row[1]
        and not bool(_safe_int(row[4]))
        and str(row[2] or "").strip() not in GENERIC_RELATIONSHIP_SECTOR_NAMES
    ]
    sector_codes = list({row[1] for row in filtered_mappings if row[1]})
    if not sector_codes:
        return {}

    persistence_result = await db.execute(
        select(
            SectorPersistence.sector_code,
            SectorPersistence.sector_name,
            SectorPersistence.strength_score,
            SectorPersistence.limit_up_count,
            SectorPersistence.fund_flow,
            SectorPersistence.change_pct,
            SectorPersistence.consecutive_days,
        ).where(
            SectorPersistence.trade_date == trade_date,
            SectorPersistence.sector_code.in_(sector_codes),
        )
    )
    persistence_map = {
        row[0]: {
            "sector_code": row[0],
            "sector_name": row[1] or "",
            "strength_score": _safe_float(row[2]),
            "limit_up_count": _safe_int(row[3]),
            "fund_flow": _safe_float(row[4]),
            "change_pct": _safe_float(row[5]),
            "consecutive_days": _safe_int(row[6]),
        }
        for row in persistence_result.all()
    }
    lifecycle_result = await db.execute(
        select(
            SectorLifecycle.sector_code,
            SectorLifecycle.lifecycle_state,
            SectorLifecycle.state_score,
            SectorLifecycle.limit_up_count,
            SectorLifecycle.consecutive_board_count,
            SectorLifecycle.fund_flow,
            SectorLifecycle.active_days,
            SectorLifecycle.quality_score,
            SectorLifecycle.is_main_line,
            SectorLifecycle.kline_trend,
        ).where(
            SectorLifecycle.trade_date == trade_date,
            SectorLifecycle.sector_code.in_(sector_codes),
        )
    )
    lifecycle_map = {
        str(row[0]): {
            "lifecycle_state": str(row[1] or ""),
            "lifecycle_score": _safe_float(row[2]),
            "attributed_limit_up_count": _safe_int(row[3]),
            "consecutive_board_count": _safe_int(row[4]),
            "lifecycle_fund_flow": _safe_float(row[5]),
            "active_days": _safe_int(row[6]),
            "quality_score": _safe_float(row[7]),
            "is_main_line": bool(_safe_int(row[8])),
            "kline_trend": str(row[9] or ""),
        }
        for row in lifecycle_result.all()
    }
    if use_live_limit_up_context is None:
        use_live_limit_up_context = trade_date == date.today()
    live_limit_up_map = (
        await _load_live_main_board_sector_limit_up_context(
            db,
            trade_date,
            [str(code or "") for code in sector_codes],
        )
        if use_live_limit_up_context
        else {}
    )
    rotation_map = await _load_sector_rotation_context(db, trade_date, [str(code or "") for code in sector_codes])

    best_by_code: dict[str, dict] = {}
    for code, sector_code, sector_name, sector_type, _is_excluded, sector_stock_count in filtered_mappings:
        base = persistence_map.get(sector_code, {})
        lifecycle = lifecycle_map.get(str(sector_code or ""), {})
        live_context = live_limit_up_map.get(str(sector_code or ""), {})
        live_limit_up_count = _safe_int(live_context.get("limit_up_count"))
        if not base and live_limit_up_count <= 0:
            continue
        # 3家及以上主板涨停已经构成盘中可验证的板块扩散证据。只给板块强度
        # 设置保守下限，资金流和连续天数仍使用真实派生值，避免把泛概念误判
        # 成持续主线。
        live_strength_floor = min(100.0, 42.0 + live_limit_up_count * 6.0)
        mapping_confidence = {
            "industry": 86.0,
            "sw_l2": 84.0,
            "sw_l3": 82.0,
            "sw_l1": 76.0,
            "concept": 62.0,
        }.get(str(sector_type or ""), 55.0)
        raw_limit_up_count = _safe_int(base.get("limit_up_count"))
        attributed_limit_up_count = _safe_int(lifecycle.get("attributed_limit_up_count"))
        effective_limit_up_count = max(
            attributed_limit_up_count if lifecycle else 0,
            live_limit_up_count,
        ) if (lifecycle or live_context) else raw_limit_up_count
        attribution_mismatch = bool(
            lifecycle
            and raw_limit_up_count >= max(effective_limit_up_count * 3, 5)
        )
        strength_score = max(
            _safe_float(base.get("strength_score")),
            live_strength_floor if live_limit_up_count >= 3 else 0.0,
        )
        if (
            attribution_mismatch
            and _safe_float(base.get("change_pct")) <= 0
            and _safe_float(base.get("fund_flow")) <= 0
        ):
            strength_score = min(strength_score, 32.0)
        candidate = {
            "sector_code": sector_code or "",
            "sector_name": base.get("sector_name") or sector_name or "",
            "sector_type": sector_type or "",
            "mapping_confidence_score": mapping_confidence,
            "sector_stock_count": _safe_int(sector_stock_count),
            "strength_score": strength_score,
            "limit_up_count": effective_limit_up_count,
            "raw_limit_up_count": raw_limit_up_count,
            "attributed_limit_up_count": effective_limit_up_count,
            "sector_attribution_verified": bool(lifecycle or live_context),
            "sector_attribution_mismatch": attribution_mismatch,
            "fund_flow": _safe_float(base.get("fund_flow")),
            "change_pct": _safe_float(base.get("change_pct")),
            "consecutive_days": _safe_int(base.get("consecutive_days")),
            "sector_live_limit_up_count": live_limit_up_count,
            "sector_live_breadth_override": live_limit_up_count >= 3,
            **lifecycle,
            **rotation_map.get(str(sector_code or ""), {}),
        }
        current = best_by_code.get(code)
        relevance = _sector_reason_relevance(
            (reason_by_code or {}).get(str(code or ""), ""),
            candidate.get("sector_name") or sector_name or "",
        )
        current_relevance = _sector_reason_relevance(
            (reason_by_code or {}).get(str(code or ""), ""),
            (current or {}).get("sector_name") or "",
        )
        candidate_activity_score = (
            _safe_float(candidate.get("strength_score")) * 0.42
            + _safe_float(candidate.get("sector_rotation_score")) * 0.23
            + _safe_float(candidate.get("lifecycle_score")) * 0.15
            + _safe_float(candidate.get("quality_score")) * 0.08
            + mapping_confidence * 0.12
            - (18.0 if attribution_mismatch else 0.0)
        )
        current_activity_score = (
            _safe_float((current or {}).get("strength_score")) * 0.42
            + _safe_float((current or {}).get("sector_rotation_score")) * 0.23
            + _safe_float((current or {}).get("lifecycle_score")) * 0.15
            + _safe_float((current or {}).get("quality_score")) * 0.08
            + _safe_float((current or {}).get("mapping_confidence_score")) * 0.12
            - (18.0 if bool((current or {}).get("sector_attribution_mismatch")) else 0.0)
        )
        candidate_primary_mapping = 1 if str(sector_type or "") in {"industry", "sw_l1", "sw_l2", "sw_l3"} else 0
        current_primary_mapping = 1 if str((current or {}).get("sector_type") or "") in {"industry", "sw_l1", "sw_l2", "sw_l3"} else 0
        candidate_rank = (
            relevance,
            # 活跃主线急跌洗盘是这条候选的来源证据；若仍机械优先主营
            # 行业，概念级洗盘上下文会在入池后丢失，导致再次被低强度挡掉。
            1 if bool(candidate.get("sector_washout_reversal_watch")) else 0,
            # 没有公告/涨停归因提示时先信主营行业，避免从几十个泛概念里
            # 机械挑当天最热的一个；有明确提示时 relevance 仍优先让真实概念胜出。
            candidate_primary_mapping if relevance == 0 else 0,
            -_safe_int(candidate.get("sector_stock_count"), 9999),
            1 if bool(candidate.get("sector_attribution_verified")) else 0,
            1 if str(candidate.get("lifecycle_state") or "") in {"emerging", "accelerating", "climax"} else 0,
            1 if bool(candidate.get("sector_low_position_rotation")) else 0,
            candidate_activity_score,
            mapping_confidence,
        )
        current_rank = (
            current_relevance,
            1 if bool((current or {}).get("sector_washout_reversal_watch")) else 0,
            current_primary_mapping if current_relevance == 0 else 0,
            -_safe_int((current or {}).get("sector_stock_count"), 9999),
            1 if bool((current or {}).get("sector_attribution_verified")) else 0,
            1 if str((current or {}).get("lifecycle_state") or "") in {"emerging", "accelerating", "climax"} else 0,
            1 if bool((current or {}).get("sector_low_position_rotation")) else 0,
            current_activity_score,
            _safe_float((current or {}).get("mapping_confidence_score")),
        )
        if current is None or candidate_rank > current_rank:
            candidate["reason_relevance"] = relevance
            candidate["sector_selection_score"] = round(candidate_activity_score, 2)
            best_by_code[code] = candidate
    return best_by_code


async def _load_spot_map(db: AsyncSession, codes: list[str]) -> dict[str, StockSpot]:
    if not codes:
        return {}
    result = await db.execute(select(StockSpot).where(StockSpot.code.in_(codes)))
    return {item.code: item for item in result.scalars().all()}


async def _load_fund_flow_map(db: AsyncSession, codes: list[str], trade_date: date) -> dict[str, FundFlow]:
    if not codes:
        return {}
    result = await db.execute(
        select(FundFlow).where(
            FundFlow.code.in_(codes),
            FundFlow.trade_date == trade_date,
        )
    )
    return {item.code: item for item in result.scalars().all()}


async def _load_fund_flow_trend_map(
    db: AsyncSession,
    codes: list[str],
    trade_date: date,
    *,
    lookback_calendar_days: int = 14,
) -> dict[str, dict]:
    """读取锚点日及之前的个股资金，构造三/五日预热而不穿越未来。"""
    normalized_codes = list(dict.fromkeys(
        str(code or "").strip() for code in codes if str(code or "").strip()
    ))
    if not normalized_codes:
        return {}
    result = await db.execute(
        select(
            FundFlow.code,
            FundFlow.trade_date,
            FundFlow.main_net_inflow,
            FundFlow.main_net_inflow_pct,
        )
        .where(
            FundFlow.code.in_(normalized_codes),
            FundFlow.trade_date <= trade_date,
            FundFlow.trade_date >= trade_date - timedelta(days=lookback_calendar_days),
        )
        .order_by(FundFlow.code, FundFlow.trade_date)
    )
    grouped: dict[str, list[dict]] = defaultdict(list)
    for code, row_trade_date, main_net_inflow, main_net_inflow_pct in result.all():
        normalized_code = str(code or "").strip()
        if not normalized_code or row_trade_date is None:
            continue
        grouped[normalized_code].append({
            "trade_date": row_trade_date,
            "main_net_inflow": _safe_float(main_net_inflow),
            "main_net_inflow_pct": _safe_float(main_net_inflow_pct),
        })
    return {
        code: build_funding_preheat_context(rows)
        for code, rows in grouped.items()
    }


def _json_list(value) -> list:
    if not value:
        return []
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def _normalize_code_list(values: list) -> list[str]:
    codes: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        for match in STOCK_CODE_RE.findall(text):
            codes.add(match)
        if text.isdigit() and len(text) <= 6:
            codes.add(text.zfill(6))
    return sorted(codes)


def _news_source_weight(source: str | None) -> float:
    """兼容旧内部调用，实际口径由新闻催化服务统一维护。"""
    return news_source_weight(source)


def _news_catalyst_score(news: FinanceNews, trade_date: date) -> float:
    return score_news_catalyst(news, trade_date)


def _is_fresh_direct_hard_news_context(context: dict | None) -> bool:
    """盘后公司级硬事件保留到观察池，但不绕过资金/K线买点闸门。"""
    payload = context or {}
    return (
        str(payload.get("news_mapping_mode") or "") == "direct_code"
        and str(payload.get("news_event_grade") or "") == "hard"
        and bool(payload.get("news_fresh_after_trade_close"))
        and _safe_float(payload.get("news_catalyst_score")) >= NEWS_CATALYST_MIN_SCORE + 8.0
    )


async def _load_sector_news_candidate_codes(
    db: AsyncSession,
    related_sectors: list[str],
    *,
    trade_date: date,
    exclude_codes: set[str],
    limit_per_sector: int = 12,
) -> list[str]:
    """非PIT当前板块研究查询；正式新闻候选已停止调用，不可用于历史补证。"""
    sector_names = list(dict.fromkeys(str(item or "").strip() for item in related_sectors if str(item or "").strip()))
    if not sector_names:
        return []
    name_filters = []
    for name in sector_names:
        name_filters.append(StockSectorMapping.sector_name == name)
        name_filters.append(SectorInfo.sector_name == name)
        if len(name) >= 2:
            name_filters.append(StockSectorMapping.sector_name.ilike(f"%{name}%"))
            name_filters.append(SectorInfo.sector_name.ilike(f"%{name}%"))
    stmt = (
        select(StockSectorMapping.code)
        .join(StockSpot, StockSpot.code == StockSectorMapping.code)
        .outerjoin(SectorInfo, StockSectorMapping.sector_code == SectorInfo.sector_code)
        .join(
            SectorPersistence,
            and_(
                SectorPersistence.sector_code == StockSectorMapping.sector_code,
                SectorPersistence.trade_date == trade_date,
            ),
        )
        .where(
            StockSectorMapping.sector_type.in_(ALLOWED_MAINLINE_SECTOR_TYPES),
            StockSpot.price > 0,
            StockSpot.change_pct >= -3.0,
            # 涨停票 change_pct 在 10.0-10.05 之间（主板 10%，一字板精确
            # 到 10.05%），原 9.2 上限会把"今日已经涨停"的候选票完全
            # 挡在外面，导致新闻催化板块扩散路径永远召回不到当日涨停
            # 龙头票。把上界放到 10.5 后能让一字板/秒板个股进入板块扩散
            # 候选池，但仍能挡住超 10.5% 的数据异常。
            StockSpot.change_pct <= 10.5,
            # 行业板块(industry/sw_*) 维持 55 分门槛，避免小杂板块污染主线；
            # 概念板块(concept) strength 普遍偏低（20-50），但 limit_up_count
            # 仍能可靠反映"今天是不是这个题材在发酵"，对新闻催化首板路线
            # 必须放低 strength 门槛，否则会把"可控核聚变/核电/商业航天"等
            # 真实涨停题材板块挡在板块过滤外。
            or_(
                and_(
                    StockSectorMapping.sector_type.in_({"industry", "sw_l1", "sw_l2", "sw_l3"}),
                    SectorPersistence.strength_score >= 55.0,
                    or_(
                        SectorPersistence.limit_up_count >= 2,
                        and_(
                            SectorPersistence.strength_score >= 65.0,
                            SectorPersistence.fund_flow > 0,
                        ),
                    ),
                ),
                and_(
                    StockSectorMapping.sector_type == "concept",
                    SectorPersistence.strength_score >= 20.0,
                    SectorPersistence.limit_up_count >= 1,
                ),
            ),
            or_(*name_filters),
        )
        .order_by(
            desc(SectorPersistence.strength_score),
            desc(SectorPersistence.limit_up_count),
            desc(StockSpot.change_pct),
            desc(StockSpot.volume_ratio),
            desc(StockSpot.support_strength_score),
            desc(StockSpot.turnover),
        )
        .limit(max(1, len(sector_names)) * limit_per_sector * 2)
    )
    if exclude_codes:
        stmt = stmt.where(~StockSectorMapping.code.in_(list(exclude_codes)))
    result = await db.execute(stmt)
    codes: list[str] = []
    for raw_code in result.scalars().all():
        code = str(raw_code or "").strip()
        if not code or code in codes or not stock_tagger.is_tradeable(code):
            continue
        codes.append(code)
        if len(codes) >= max(1, len(sector_names)) * limit_per_sector:
            break
    return codes


async def _load_news_catalyst_context_map(
    db: AsyncSession,
    trade_date: date,
    *,
    exclude_codes: set[str],
    limit: int = 300,
    news_end_time: datetime | None = None,
) -> dict[str, dict]:
    # 与雷达共用不可变新闻/分析和实体校验，不能另查可覆盖FinanceNews补候选。
    direct_context_map = await load_direct_stock_catalyst_map(
        db,
        trade_date,
        limit=max(limit * 2, 600),
        min_score=NEWS_CATALYST_MIN_SCORE,
        news_end_time=news_end_time,
    )
    context_map: dict[str, dict] = {
        code: {
            **context,
            "news_sector_inferred": False,
            "news_mapping_mode": "direct_code",
        }
        for code, context in direct_context_map.items()
        if code not in exclude_codes
    }

    # SectorPersistence只有交易日期，成员映射/StockSpot也不是不可变历史版本。
    # 即使新闻本身通过PIT，当前板块强度、成员和行情仍不能证明截止时点已知。
    # 暂停这条sector_inferred旁路；保留其他有独立证据的主线/竞价路线和阈值。
    # 缺证原因由news_evidence_gate返回，不能把“未获证明”呈现为“市场没有新闻”。
    return context_map


async def _resolve_live_promotion_news_end_time(
    trade_date: date,
    *,
    now: datetime | None = None,
) -> datetime | None:
    """Return the information cutoff for an ad-hoc live view without future timestamps."""
    current = now or datetime.now()
    if current.date() < trade_date:
        return current
    if current.date() == trade_date:
        return current
    next_trade_date = await trade_calendar.next_trade_day(trade_date)
    if current.date() > next_trade_date:
        # Historical views use that trade day's natural midnight boundary.
        return None
    pre_auction_cutoff = datetime.combine(next_trade_date, datetime.min.time()).replace(
        hour=9,
        minute=25,
    )
    return min(current, pre_auction_cutoff)


async def _resolve_promotion_snapshot_news_end_time(
    trade_date: date,
    *,
    snapshot_source: str,
    snapshot_context: str,
    now: datetime | None = None,
) -> datetime | None:
    """Pin schedule snapshots to their own clock instead of the request execution time."""
    current = now or datetime.now()
    normalized_source = _normalize_prediction_snapshot_source(snapshot_source)
    context = str(snapshot_context or "").strip().lower()
    if normalized_source != "schedule":
        return await _resolve_live_promotion_news_end_time(trade_date, now=current)

    close_clock = {
        "promotion_1510": (trade_date, 15, 10),
        "promotion_2000": (trade_date, 20, 0),
    }.get(context)
    if close_clock:
        clock_date, hour, minute = close_clock
        cutoff = datetime.combine(clock_date, datetime.min.time()).replace(
            hour=hour,
            minute=minute,
        )
        return min(current, cutoff)

    if context in PROMOTION_INTRADAY_CONTEXTS:
        intraday_clock = {
            "promotion_0925": (9, 25),
            "promotion_0935": (9, 35),
            "promotion_1000": (10, 0),
            "promotion_1030": (10, 30),
            "promotion_1305": (13, 5),
            "promotion_1400": (14, 0),
            "promotion_1430": (14, 30),
        }
        session_date = (
            trade_date
            if trade_date == current.date()
            else await trade_calendar.next_trade_day(trade_date)
        )
        hour, minute = intraday_clock[context]
        cutoff = datetime.combine(session_date, datetime.min.time()).replace(
            hour=hour,
            minute=minute,
        )
        return min(current, cutoff)

    return await _resolve_live_promotion_news_end_time(trade_date, now=current)


async def _load_auction_surge_context_map(
    db: AsyncSession,
    trade_date: date,
    *,
    exclude_codes: set[str],
    limit: int = 1200,
    as_of_at: datetime | None = None,
) -> dict[str, dict]:
    decision_at = as_of_at if as_of_at is not None else datetime.now()
    # 竞价是逐分钟快照。旧实现按涨幅倒序后保留历史最高值，会把09:15
    # 的虚假顶板当作09:25最终竞价，既制造诱多也漏掉临近结束才集体转强
    # 的板块。先锁定每只股票最新一帧，再做个股和板块宽度判断。
    latest_time_subquery = (
        select(
            AuctionData.code.label("code"),
            func.max(AuctionData.auction_time).label("latest_auction_time"),
        )
        .where(
            AuctionData.trade_date == trade_date,
            AuctionData.auction_time.between("09:15:00", "09:25:30"),
        )
        .group_by(AuctionData.code)
        .subquery()
    )
    result = await db.execute(
        select(AuctionData)
        .join(
            latest_time_subquery,
            and_(
                latest_time_subquery.c.code == AuctionData.code,
                latest_time_subquery.c.latest_auction_time == AuctionData.auction_time,
            ),
        )
        .where(
            AuctionData.trade_date == trade_date,
            # 板块集体修复时允许把1%~2.6%的跟随成员纳入预测观察，但
            # 单股强攻和可交易闸门仍沿用2.6%的原阈值。
            AuctionData.open_change >= 1.0,
        )
        .order_by(desc(AuctionData.open_change), desc(AuctionData.volume_ratio), desc(AuctionData.auction_amount))
        .limit(limit)
    )
    latest_rows: dict[str, AuctionData] = {}
    for item in result.scalars().all():
        code = str(getattr(item, "code", "") or "").strip()
        if (
            not code
            or code in exclude_codes
            or code in latest_rows
            or not stock_tagger.is_tradeable(code)
            or auction_evidence_status(item, decision_at=decision_at) == "future"
        ):
            continue
        latest_rows[code] = item

    # 只看09:25最后一帧会漏掉“竞价深水快速翻红”的弱转强确认。保存09:20
    # 前最后一帧用于计算轨迹；轨迹只能生成预测种子，成交额/量比缺失时仍
    # 不得变成 trade_ready，避免把撤单造成的虚假翻红当作买点。
    baseline_rows: dict[str, AuctionData] = {}
    if latest_rows:
        baseline_time_subquery = (
            select(
                AuctionData.code.label("code"),
                func.max(AuctionData.auction_time).label("baseline_auction_time"),
            )
            .where(
                AuctionData.trade_date == trade_date,
                AuctionData.auction_time.between("09:15:00", "09:20:30"),
                AuctionData.auction_price > 0,
                AuctionData.prev_close > 0,
                AuctionData.code.in_(list(latest_rows.keys())),
            )
            .group_by(AuctionData.code)
            .subquery()
        )
        baseline_result = await db.execute(
            select(AuctionData)
            .join(
                baseline_time_subquery,
                and_(
                    baseline_time_subquery.c.code == AuctionData.code,
                    baseline_time_subquery.c.baseline_auction_time == AuctionData.auction_time,
                ),
            )
            .where(AuctionData.trade_date == trade_date)
        )
        baseline_rows = {
            str(item.code or "").strip(): item
            for item in baseline_result.scalars().all()
            if str(item.code or "").strip()
            and auction_evidence_status(item, decision_at=decision_at) != "future"
        }

    sector_cluster_by_code: dict[str, dict] = {}
    if latest_rows:
        mapping_result = await db.execute(
            select(
                StockSectorMapping.code,
                StockSectorMapping.sector_code,
                StockSectorMapping.sector_name,
                StockSectorMapping.sector_type,
                SectorInfo.is_excluded,
            )
            .outerjoin(SectorInfo, StockSectorMapping.sector_code == SectorInfo.sector_code)
            .where(
                StockSectorMapping.code.in_(list(latest_rows.keys())),
                StockSectorMapping.sector_type.in_(ALLOWED_MAINLINE_SECTOR_TYPES),
            )
        )
        sector_buckets: dict[str, dict] = {}
        code_sector_keys: dict[str, list[str]] = defaultdict(list)
        for code, sector_code, sector_name, sector_type, is_excluded in mapping_result.all():
            normalized_code = str(code or "").strip()
            normalized_sector_code = str(sector_code or "").strip()
            normalized_sector_name = str(sector_name or "").strip()
            if (
                not normalized_code
                or not normalized_sector_code
                or not normalized_sector_name
                or bool(_safe_int(is_excluded))
                or normalized_sector_name in GENERIC_RELATIONSHIP_SECTOR_NAMES
            ):
                continue
            code_sector_keys[normalized_code].append(normalized_sector_code)
            item = latest_rows.get(normalized_code)
            open_change = _safe_float(getattr(item, "open_change", 0.0))
            if open_change < AUCTION_SURGE_MIN_OPEN_CHANGE:
                continue
            bucket = sector_buckets.setdefault(
                normalized_sector_code,
                {
                    "sector_name": normalized_sector_name,
                    "sector_type": str(sector_type or ""),
                    "member_codes": set(),
                    "open_changes": [],
                },
            )
            if normalized_code not in bucket["member_codes"]:
                bucket["member_codes"].add(normalized_code)
                bucket["open_changes"].append(open_change)

        confirmed_clusters: dict[str, dict] = {}
        for sector_code, bucket in sector_buckets.items():
            member_count = len(bucket["member_codes"])
            average_open = _mean(bucket["open_changes"])
            max_open = max(bucket["open_changes"] or [0.0])
            if member_count < 4 or average_open < 3.2 or max_open < 7.0:
                continue
            confirmed_clusters[sector_code] = {
                "auction_sector_code": sector_code,
                "auction_sector_name": bucket["sector_name"],
                "auction_sector_type": bucket["sector_type"],
                "auction_sector_member_count": member_count,
                "auction_sector_avg_open_change": round(average_open, 2),
                "auction_sector_max_open_change": round(max_open, 2),
                "auction_sector_member_codes": sorted(bucket["member_codes"]),
            }

        for code, sector_keys in code_sector_keys.items():
            matched = [confirmed_clusters[key] for key in sector_keys if key in confirmed_clusters]
            if not matched:
                continue
            sector_cluster_by_code[code] = max(
                matched,
                key=lambda item: (
                    _safe_int(item.get("auction_sector_member_count")),
                    _safe_float(item.get("auction_sector_avg_open_change")),
                    1 if str(item.get("auction_sector_type")) in {"industry", "sw_l1", "sw_l2", "sw_l3"} else 0,
                ),
            )

    context_map: dict[str, dict] = {}
    for code, item in latest_rows.items():
        code = str(getattr(item, "code", "") or "").strip()
        open_change = _safe_float(getattr(item, "open_change", 0.0))
        baseline_item = baseline_rows.get(code)
        baseline_open_change = _safe_float(
            getattr(baseline_item, "open_change", open_change),
            open_change,
        )
        open_change_delta = open_change - baseline_open_change
        reversal_confirmed = bool(
            baseline_item is not None
            and baseline_open_change <= 0.0
            and open_change >= 1.0
            and open_change_delta >= 3.0
            and not bool(getattr(item, "is_cancelled", False))
        )
        # None 代表数据源没有给出竞价增量成交，不能把它按 1.0 倍量
        # 处理成“成交字段完整”。否则虚单集群会绕过只观察闸门。
        volume_ratio = _safe_float(getattr(item, "volume_ratio", 0.0), 0.0)
        auction_volume = _safe_float(getattr(item, "auction_volume", 0.0))
        auction_amount = _safe_float(getattr(item, "auction_amount", 0.0))
        evidence_status = auction_evidence_status(item, decision_at=decision_at)
        feed_complete = evidence_status == "ok"
        individual_strength = _clamp_score(
            min(max(open_change, 0.0), 10.0) * 5.0
            + min(max(volume_ratio, 0.0), 6.0) * 7.0
            + min(auction_amount / 20_000_000, 1.0) * 18.0
            - (18.0 if bool(getattr(item, "is_cancelled", False)) else 0.0)
        )
        trajectory_strength = (
            min(72.0, 38.0 + min(open_change_delta, 10.0) * 2.2)
            if reversal_confirmed
            else 0.0
        )
        cluster_context = sector_cluster_by_code.get(code) or {}
        cluster_confirmed = bool(cluster_context)
        cluster_strength = 0.0
        if cluster_confirmed:
            cluster_strength = min(
                78.0,
                42.0
                + min(max(_safe_int(cluster_context.get("auction_sector_member_count")) - 3, 0), 8) * 2.5
                + min(_safe_float(cluster_context.get("auction_sector_avg_open_change")), 6.0) * 1.5
                + min(max(open_change, 0.0), 4.0) * 1.5,
            )
        strength = _clamp_score(max(individual_strength, cluster_strength, trajectory_strength))
        if strength < AUCTION_SURGE_MIN_SCORE:
            continue
        context_map[code] = {
            "auction_strength_score": round(strength, 2),
            "auction_individual_strength_score": round(individual_strength, 2),
            "auction_open_change": round(open_change, 2),
            "auction_baseline_open_change": round(baseline_open_change, 2),
            "auction_open_change_delta": round(open_change_delta, 2),
            "auction_reversal_confirmed": reversal_confirmed,
            "auction_volume_ratio": round(volume_ratio, 3),
            "auction_volume": round(auction_volume, 2),
            "auction_amount": round(auction_amount, 2),
            "auction_price": _safe_float(getattr(item, "auction_price", 0.0)),
            "auction_time": str(getattr(item, "auction_time", "") or ""),
            "auction_trade_date": str(trade_date),
            "auction_cancelled": bool(getattr(item, "is_cancelled", False)),
            "auction_feed_complete": feed_complete,
            "auction_evidence_status": evidence_status,
            "auction_evidence_contract": "auction_provenance_v1",
            "auction_cluster_confirmed": cluster_confirmed,
            # 无增量成交字段时只做预测种子，防止竞价虚单直接触发交易。
            "prediction_shape_seed": bool(
                (cluster_confirmed or reversal_confirmed) and not feed_complete
            ),
            **cluster_context,
        }
    return context_map


def _score_board_memory_reset_pattern(
    bars: list[dict],
    *,
    code: str = "",
    name: str = "",
) -> dict:
    """识别“历史涨停记忆 + 低位缩量回撤”的宽候选种子。

    该形态在逐日复盘里能明显补召回，但单独使用的次日涨停精度仍低，
    因此这里只生成 ``prediction_shape_seed``，不能直接生成交易买点。盘中
    仍需同题材竞价集群、直接硬消息或滚动量价确认完成升格。
    """
    valid_bars = [
        item
        for item in bars
        if _safe_float(item.get("close")) > 0
        and _safe_float(item.get("high")) > 0
        and _safe_float(item.get("low")) > 0
    ]
    if len(valid_bars) < 60:
        return {
            "board_memory_reset_ready": False,
            "board_memory_reset_score": 0.0,
        }

    latest = valid_bars[-1]
    prior_bars = valid_bars[:-1]
    board_indices = [
        index
        for index, item in enumerate(prior_bars)
        if _is_limit_up_change(code, name, _safe_float(item.get("change_pct")))
    ]
    if not board_indices:
        return {
            "board_memory_reset_ready": False,
            "board_memory_reset_score": 0.0,
        }

    last_board_index = board_indices[-1]
    board_age = len(valid_bars) - 1 - last_board_index
    close_price = _safe_float(latest.get("close"))
    long_window = valid_bars[-120:]
    long_high = max(_safe_float(item.get("high")) for item in long_window)
    long_low = min(_safe_float(item.get("low")) for item in long_window)
    position_120 = (
        (close_price - long_low) / (long_high - long_low)
        if long_high > long_low
        else 1.0
    )
    closes = [_safe_float(item.get("close")) for item in valid_bars]
    previous_volumes = [
        _safe_float(item.get("volume"))
        for item in prior_bars[-20:]
        if _safe_float(item.get("volume")) > 0
    ]
    avg_volume_20 = _mean(previous_volumes)
    volume_ratio_20 = (
        _safe_float(latest.get("volume")) / avg_volume_20
        if avg_volume_20 > 0
        else 0.0
    )
    ma20 = _mean(closes[-20:])
    return_5d = _safe_percent_change(close_price, closes[-6]) if len(closes) >= 6 else 0.0
    return_20d = _safe_percent_change(close_price, closes[-21]) if len(closes) >= 21 else 0.0
    distance_ma20 = _safe_percent_change(close_price, ma20) if ma20 > 0 else 0.0
    high_20 = max(_safe_float(item.get("high")) for item in valid_bars[-20:])
    high_20_gap = _safe_percent_change(close_price, high_20) if high_20 > 0 else 0.0
    latest_change = _safe_float(latest.get("change_pct"))

    ready = bool(
        3 <= board_age <= 45
        and position_120 <= 0.70
        and -16.0 <= return_5d <= 6.0
        and -22.0 <= return_20d <= 30.0
        and 0.30 <= volume_ratio_20 <= 1.85
        and -16.0 <= distance_ma20 <= 16.0
        and -28.0 <= high_20_gap <= -2.0
        and -8.8 <= latest_change <= 5.5
    )
    score = 0.0
    if ready:
        score = _clamp_score(
            _range_score(board_age, ideal_low=5.0, ideal_high=18.0, min_value=3.0, max_value=45.0) * 0.18
            + _inverse_score(position_120 * 100.0, good=18.0, bad=70.0) * 0.22
            + _range_score(return_5d, ideal_low=-8.0, ideal_high=1.5, min_value=-16.0, max_value=6.0) * 0.17
            + _range_score(return_20d, ideal_low=-4.0, ideal_high=14.0, min_value=-22.0, max_value=30.0) * 0.10
            + _range_score(volume_ratio_20, ideal_low=0.45, ideal_high=1.15, min_value=0.30, max_value=1.85) * 0.14
            + _range_score(distance_ma20, ideal_low=-5.0, ideal_high=6.0, min_value=-16.0, max_value=16.0) * 0.08
            + _range_score(high_20_gap, ideal_low=-18.0, ideal_high=-4.0, min_value=-28.0, max_value=-2.0) * 0.11
        )
    last_board = prior_bars[last_board_index]
    return {
        "board_memory_reset_ready": ready,
        "board_memory_reset_score": round(score, 2),
        "board_memory_days_since_limit_up": board_age,
        "board_memory_last_limit_up_date": str(last_board.get("trade_date") or ""),
        "board_memory_position_120": round(position_120, 4),
        "board_memory_return_5d": round(return_5d, 2),
        "board_memory_return_20d": round(return_20d, 2),
        "board_memory_volume_ratio_20": round(volume_ratio_20, 3),
        "board_memory_distance_ma20": round(distance_ma20, 2),
        "board_memory_high_20_gap": round(high_20_gap, 2),
    }


def _score_low_base_rotation_pattern(
    bars: list[dict],
    *,
    code: str = "",
    name: str = "",
) -> dict:
    """识别“无近期涨停记忆的低位首波”宽召回种子。

    最近多日首板复盘显示，除了历史涨停记忆回撤外，还有一批股票在
    120 日低位、20 日温和修复后被新题材轮动点火。该形态全市场同类
    样本很多，单独使用精度很低，因此与记忆回撤一样只扩充预测召回；
    没有直接硬消息、竞价题材集群或盘中增量成交时不能升格为买点。
    """
    valid_bars = [
        item
        for item in bars
        if _safe_float(item.get("close")) > 0
        and _safe_float(item.get("high")) > 0
        and _safe_float(item.get("low")) > 0
        and _safe_float(item.get("volume")) > 0
    ]
    if len(valid_bars) < 80:
        return {
            "low_base_rotation_ready": False,
            "low_base_rotation_score": 0.0,
        }

    latest = valid_bars[-1]
    closes = [_safe_float(item.get("close")) for item in valid_bars]
    volumes = [_safe_float(item.get("volume")) for item in valid_bars]
    recent_changes = [
        _safe_float(item.get("change_pct"))
        for item in valid_bars[-31:]
    ]
    if any(_is_limit_up_change(code, name, change_pct) for change_pct in recent_changes):
        return {
            "low_base_rotation_ready": False,
            "low_base_rotation_score": 0.0,
        }

    close_price = closes[-1]
    long_window = valid_bars[-120:]
    long_high = max(_safe_float(item.get("high")) for item in long_window)
    long_low = min(_safe_float(item.get("low")) for item in long_window)
    position_120 = (
        (close_price - long_low) / (long_high - long_low)
        if long_high > long_low
        else 1.0
    )
    return_5d = _safe_percent_change(close_price, closes[-6])
    return_20d = _safe_percent_change(close_price, closes[-21])
    return_60d = _safe_percent_change(close_price, closes[-61])
    ma20 = _mean(closes[-20:])
    distance_ma20 = _safe_percent_change(close_price, ma20) if ma20 > 0 else 0.0
    high_20 = max(_safe_float(item.get("high")) for item in valid_bars[-20:])
    high_20_gap = _safe_percent_change(close_price, high_20) if high_20 > 0 else 0.0
    avg_volume_5 = _mean(volumes[-5:])
    avg_volume_20 = _mean(volumes[-20:])
    volume_ratio_5_20 = avg_volume_5 / avg_volume_20 if avg_volume_20 > 0 else 0.0
    prior_20_changes = [_safe_float(item.get("change_pct")) for item in valid_bars[-21:-1]]
    prior_impulse = max(prior_20_changes or [0.0])
    latest_change = _safe_float(latest.get("change_pct"))

    ready = bool(
        position_120 <= 0.42
        and -8.0 <= return_5d <= 4.0
        and -12.0 <= return_20d <= 22.0
        and -35.0 <= return_60d <= 10.0
        and 0.45 <= volume_ratio_5_20 <= 1.60
        and -10.0 <= distance_ma20 <= 10.0
        and -25.0 <= high_20_gap <= -3.0
        and prior_impulse >= 3.5
        and -8.8 <= latest_change <= 5.5
    )
    score = 0.0
    if ready:
        score = _clamp_score(
            46.0
            + _inverse_score(position_120 * 100.0, good=12.0, bad=42.0) * 0.15
            + _range_score(return_5d, ideal_low=-5.0, ideal_high=1.0, min_value=-8.0, max_value=4.0) * 0.11
            + _range_score(return_20d, ideal_low=0.0, ideal_high=12.0, min_value=-12.0, max_value=22.0) * 0.08
            + _range_score(volume_ratio_5_20, ideal_low=0.62, ideal_high=1.08, min_value=0.45, max_value=1.60) * 0.08
            + _range_score(high_20_gap, ideal_low=-16.0, ideal_high=-5.0, min_value=-25.0, max_value=-3.0) * 0.07
            + _range_score(prior_impulse, ideal_low=4.0, ideal_high=7.5, min_value=3.5, max_value=9.4) * 0.05
        )
    return {
        "low_base_rotation_ready": ready,
        "low_base_rotation_score": round(score, 2),
        "low_base_position_120": round(position_120, 4),
        "low_base_return_5d": round(return_5d, 2),
        "low_base_return_20d": round(return_20d, 2),
        "low_base_return_60d": round(return_60d, 2),
        "low_base_volume_ratio_5_20": round(volume_ratio_5_20, 3),
        "low_base_distance_ma20": round(distance_ma20, 2),
        "low_base_high_20_gap": round(high_20_gap, 2),
        "low_base_prior_impulse": round(prior_impulse, 2),
    }


def _pre_board_probe_effective_limit(limit: int, market_washout_setup: bool) -> int:
    normalized_limit = max(_safe_int(limit), 0)
    if not market_washout_setup:
        return normalized_limit
    return max(
        normalized_limit,
        min(2400, round(normalized_limit * 4 / 3)),
    )


async def _get_exact_next_observed_market_date(
    db: AsyncSession,
    trade_date: date,
) -> date | None:
    """返回数据库中 T 之后最早的可观测交易日。

    历史回放只能在 ``StockSpot.updated_at`` 恰好属于这个 T+1 市场日时，
    才可把 spot.prev_close 当作 T 日最终收盘价修复源。仅按自然日间隔判断会
    在缺少 T+1 快照时误用 T+2/T+3 的实时数据，形成严重未来函数。
    """
    observed_dates: list[date] = []
    for model in (StockKline, AuctionData, LimitUpPool):
        result = await db.execute(
            select(func.min(model.trade_date)).where(model.trade_date > trade_date)
        )
        next_date = result.scalar_one_or_none()
        if next_date is not None:
            observed_dates.append(next_date)
    return min(observed_dates) if observed_dates else None


async def _load_pre_board_probe_context_map(
    db: AsyncSession,
    trade_date: date,
    *,
    exclude_codes: set[str],
    market_context: dict | None = None,
    limit: int = PRE_BOARD_SCAN_LIMIT,
) -> dict[str, dict]:
    market_context = market_context or {}
    market_washout_setup = bool(market_context.get("washout_reversal_setup"))
    latest_result = await db.execute(
        select(
            StockKline.code,
            StockKline.open,
            StockKline.close,
            StockKline.high,
            StockKline.low,
            StockKline.volume,
            StockKline.amount,
            StockKline.turnover,
            StockKline.change_pct,
            StockKline.prev_close,
        ).where(
            StockKline.trade_date == trade_date,
            StockKline.close > 0,
            StockKline.high > 0,
            StockKline.low > 0,
            StockKline.change_pct >= -10.5,
            StockKline.change_pct <= 8.8,
            StockKline.turnover >= 0.4,
            # 强趋势首阴深洗常伴随高换手；这里只放宽日K召回，后续仍须
            # 通过趋势记忆和次日盘中回收确认，不能据此直接生成买点。
            StockKline.turnover <= 40.0,
        )
    )
    latest_rows: dict[str, dict] = {}
    for row in latest_result.all():
        code = str(row[0] or "").strip()
        # 晋级预测与牛股雷达正式池只交易主板。必须在全市场历史 K 线
        # 查询之前过滤，避免创业板/科创板候选占满 900 个形态槽位后，
        # 主板真正的预热样本反而没有机会进入后续排序。
        if not code or code in exclude_codes or not stock_tagger.is_tradeable(code):
            continue
        latest_rows[code] = {
            "code": code,
            "open": _safe_float(row[1]),
            "close": _safe_float(row[2]),
            "high": _safe_float(row[3]),
            "low": _safe_float(row[4]),
            "volume": _safe_float(row[5]),
            "amount": _safe_float(row[6]),
            "turnover": _safe_float(row[7]),
            "change_pct": _safe_float(row[8]),
            "prev_close": _safe_float(row[9]),
        }
    if not latest_rows:
        return {}

    exact_next_observed_market_date = await _get_exact_next_observed_market_date(
        db,
        trade_date,
    )
    codes = list(latest_rows.keys())
    history_result = await db.execute(
        select(
            StockKline.code,
            StockKline.trade_date,
            StockKline.open,
            StockKline.close,
            StockKline.high,
            StockKline.low,
            StockKline.volume,
            StockKline.amount,
            StockKline.turnover,
            StockKline.change_pct,
            StockKline.prev_close,
        )
        .where(
            StockKline.code.in_(codes),
            StockKline.trade_date < trade_date,
            # 需要至少120根时点可见K线识别“中期动量→缩量洗盘”；
            # 210个自然日通常覆盖约145个交易日，不读取更长全市场历史。
            StockKline.trade_date >= trade_date - timedelta(days=210),
            StockKline.close > 0,
            StockKline.high > 0,
            StockKline.low > 0,
            StockKline.volume > 0,
        )
        .order_by(StockKline.code, StockKline.trade_date)
    )
    history_bars: dict[str, list[dict]] = {}
    for row in history_result.all():
        normalized_code = str(row[0] or "").strip()
        if not normalized_code:
            continue
        history_bars.setdefault(normalized_code, []).append(
            {
                "code": normalized_code,
                "trade_date": row[1],
                "open": _safe_float(row[2]),
                "close": _safe_float(row[3]),
                "high": _safe_float(row[4]),
                "low": _safe_float(row[5]),
                "volume": _safe_float(row[6]),
                "amount": _safe_float(row[7]),
                "turnover": _safe_float(row[8]),
                "change_pct": _safe_float(row[9]),
                "prev_close": _safe_float(row[10]),
            }
        )

    name_result = await db.execute(
        select(
            StockSpot.code,
            StockSpot.name,
            StockSpot.prev_close,
            StockSpot.updated_at,
        ).where(StockSpot.code.in_(codes))
    )
    spot_meta_map = {
        str(code or ""): {
            "name": str(name or ""),
            "prev_close": _safe_float(prev_close),
            "updated_at": updated_at,
        }
        for code, name, prev_close, updated_at in name_result.all()
    }

    candidates: list[tuple[float, str, dict]] = []
    for code, latest_row in latest_rows.items():
        latest = dict(latest_row)
        spot_meta = spot_meta_map.get(code) or {}
        spot_updated_at = spot_meta.get("updated_at")
        spot_snapshot_date = _parse_iso_trade_date(spot_updated_at)
        kline_close_reconciled = False
        if (
            exact_next_observed_market_date is not None
            and spot_snapshot_date == exact_next_observed_market_date
            and _safe_float(spot_meta.get("prev_close")) > 0
            and _safe_float(latest.get("close")) > 0
            and abs(
                _safe_float(spot_meta.get("prev_close"))
                / _safe_float(latest.get("close"))
                - 1.0
            )
            >= 0.001
        ):
            latest["close"] = _safe_float(spot_meta.get("prev_close"))
            latest["high"] = max(
                _safe_float(latest.get("high")),
                _safe_float(latest.get("close")),
            )
            latest["low"] = min(
                _safe_float(latest.get("low")),
                _safe_float(latest.get("close")),
            )
            latest["change_pct"] = _safe_percent_change(
                _safe_float(latest.get("close")),
                _safe_float(latest.get("prev_close")),
            )
            kline_close_reconciled = True
        prev_close = _safe_float(latest.get("prev_close"))
        close_price = _safe_float(latest.get("close"))
        high_price = _safe_float(latest.get("high"))
        low_price = _safe_float(latest.get("low"))
        open_price = _safe_float(latest.get("open"))
        if prev_close <= 0 or close_price <= 0 or high_price <= low_price:
            continue

        change_pct = _safe_float(latest.get("change_pct"))
        if change_pct == 0:
            change_pct = _safe_percent_change(close_price, prev_close)
        turnover = _safe_float(latest.get("turnover"))
        previous_bars = history_bars.get(code, [])
        previous_volumes = [_safe_float(item.get("volume")) for item in previous_bars[-5:] if _safe_float(item.get("volume")) > 0]
        avg_previous_volume = _mean(previous_volumes)
        volume_ratio = _safe_float(latest.get("volume")) / avg_previous_volume if avg_previous_volume > 0 else 0.0
        amplitude = (high_price - low_price) / prev_close * 100.0 if prev_close > 0 else 0.0
        close_position = (close_price - low_price) / (high_price - low_price) if high_price > low_price else 0.0
        intraday_high_pct = _safe_percent_change(high_price, prev_close)
        upper_gap_pct = (high_price - close_price) / close_price * 100.0 if close_price > 0 else 0.0
        lower_gap_pct = (close_price - low_price) / close_price * 100.0 if close_price > 0 else 0.0
        full_bars = [*previous_bars[-139:], {**latest, "trade_date": trade_date}]
        stock_name = str(spot_meta.get("name") or code)
        board_memory_meta = _score_board_memory_reset_pattern(
            full_bars,
            code=code,
            name=stock_name,
        )
        board_memory_reset_ready = bool(board_memory_meta.get("board_memory_reset_ready"))
        low_base_rotation_meta = _score_low_base_rotation_pattern(
            full_bars,
            code=code,
            name=stock_name,
        )
        low_base_rotation_ready = bool(low_base_rotation_meta.get("low_base_rotation_ready"))
        burst_pullback_meta = _build_burst_pullback_restart_meta(full_bars)
        burst_pullback_ready = bool(burst_pullback_meta.get("burst_pullback_restart_ready"))
        momentum_shakeout_meta = _score_momentum_shakeout_kline_pattern(
            closes=[_safe_float(item.get("close")) for item in full_bars],
            highs=[_safe_float(item.get("high")) for item in full_bars],
            lows=[_safe_float(item.get("low")) for item in full_bars],
            volumes=[_safe_float(item.get("volume")) for item in full_bars],
            change_pct=change_pct,
            turnover=turnover,
        )
        momentum_shakeout_ready = bool(
            momentum_shakeout_meta.get("is_momentum_shakeout_pattern")
        )
        strong_first_bearish_meta = _main_wave_pullback_shape_stats(
            opens=[_safe_float(item.get("open")) for item in full_bars],
            closes=[_safe_float(item.get("close")) for item in full_bars],
            highs=[_safe_float(item.get("high")) for item in full_bars],
            lows=[_safe_float(item.get("low")) for item in full_bars],
            volumes=[_safe_float(item.get("volume")) for item in full_bars],
        )
        strong_first_bearish_ready = bool(
            strong_first_bearish_meta.get("pullback_shape_ready")
            and str(strong_first_bearish_meta.get("pullback_quality_tier") or "")
            == "reclaim"
            and _safe_float(
                strong_first_bearish_meta.get(
                    "pullback_technical_persistence_score"
                )
            )
            >= 66.0
        )
        strong_first_bearish_score = (
            _clamp_score(
                50.0
                + _safe_float(
                    strong_first_bearish_meta.get(
                        "pullback_technical_persistence_score"
                    )
                )
                * 0.35
                + min(
                    _safe_float(strong_first_bearish_meta.get("pullback_prior_gain_pct_20")),
                    60.0,
                )
                * 0.10
                + min(
                    _safe_float(strong_first_bearish_meta.get("pullback_prior_impulse_days_10")),
                    4.0,
                )
                * 1.5
                + max(
                    0.0,
                    2.3
                    - _safe_float(strong_first_bearish_meta.get("pullback_volume_contraction")),
                )
                * 1.2
            )
            if strong_first_bearish_ready
            else 0.0
        )
        history_closes = [_safe_float(item.get("close")) for item in full_bars if _safe_float(item.get("close")) > 0]
        history_highs = [_safe_float(item.get("high")) for item in full_bars if _safe_float(item.get("high")) > 0]
        ma5 = _mean(history_closes[-5:]) if len(history_closes) >= 5 else 0.0
        ma10 = _mean(history_closes[-10:]) if len(history_closes) >= 10 else 0.0
        ma20 = _mean(history_closes[-20:]) if len(history_closes) >= 20 else 0.0
        recent_reference_high = max(history_highs[-21:-1]) if len(history_highs) >= 21 else max(history_highs[:-1] or [0.0])
        trend_preheat = bool(
            recent_reference_high > 0
            and close_price >= recent_reference_high * 0.985
            and ma5 > 0
            and ma10 > 0
            and close_price >= ma5 * 0.99
            and ma5 >= ma10 * 0.995
            and (ma20 <= 0 or ma10 >= ma20 * 0.985)
            and 1.05 <= volume_ratio <= 3.8
            and 1.2 <= change_pct <= 8.8
            and close_position >= 0.52
        )
        market_washout_reversal_seed = bool(
            market_washout_setup
            and -8.8 <= change_pct <= -1.2
            and turnover >= 0.8
            and amplitude >= 3.0
            and close_position >= 0.16
        )
        washout_reversal = bool(
            (change_pct <= -2.5 and upper_gap_pct >= 2.0 and turnover >= 1.0)
            or market_washout_reversal_seed
        )
        panic_flush = change_pct <= -4.0 and turnover >= 1.0 and amplitude >= 4.5
        limit_probe = intraday_high_pct >= 6.0 and change_pct < 8.8 and turnover >= 1.0
        # 预热放量预热门槛放宽：温和上涨(>=0.6%) + 强势收(>=0.55) + 正常活跃
        # (换手>=0.8, 量比>=0.7) 即可，避免把前一日温和整理但尾盘强势收的
        # 真实首板预备票挡在召回外。该放宽只放宽入口门槛，score 仍按多
        # 因子加权综合评估，不会直接拉高虚假精度。复盘显示 8/26 涨停票中
        # 002366/600468/603118 等涨幅 1.5%+、cp 0.77-0.92 的票 vr 仅 0.67-0.73
        # 仍真实涨停，旧 vr>=1.0 门槛把这类优质票挡在召回外。
        preheat_breakout = change_pct >= 0.6 and volume_ratio >= 0.7 and close_position >= 0.55 and turnover >= 0.8
        # 摸板回落放宽 close_position 下限到 0.05，让开盘冲板后大幅回落
        # 但未跌破均线/开盘支撑的票也能进入召回（典型“冲板失败次日反包”
        # 形态，前一日 close_pos 在 0.08~0.18 区间最常见）。
        touch_board_pullback = bool(
            limit_probe
            and intraday_high_pct >= 8.0
            and (upper_gap_pct >= 3.2 or close_position <= 0.48)
            and close_position >= 0.05
            and change_pct > -5.5
            and volume_ratio <= 8.0
        )
        # 新增“温和整理 + 强势收”通路：前一日振幅<5% 且尾盘收在高位
        # (close_pos>=0.85)，常见于强势题材启动前的蓄势整理。该通路只
        # 用于召回，不直接抬高买点；通过 pre_board_probe_score 之后
        # 才能进入正式候选池。
        quiet_setup_seed = bool(
            amplitude <= 4.5
            and close_position >= 0.85
            and abs(change_pct) <= 1.2
            and turnover >= 0.6
            and volume_ratio >= 0.70
        )
        # 新增"低位温和异动预备"通路：识别前一日"温和上涨 0.5-3%、强势收
        # cp>=0.65、换手>=1.5、vr>=0.8" 的样本，对应法尔胜/融发核电/共进
        # 股份/百利电气/宝鹰股份这类真实首板预备票。复盘 8/26 涨停样本
        # 中此类样本占总样本约 15%，旧 preheat_breakout 因为 cp/vr/turn 偏
        # 低阈值未覆盖。该通路仅作召回使用，需通过 pre_board_probe_score
        # 阈值排序后才能进正式候选池。
        mild_anomaly_seed = bool(
            0.5 <= change_pct <= 4.5
            and close_position >= 0.65
            and turnover >= 1.5
            and volume_ratio >= 0.7
        )
        # 新增"弱势整理后准备反包"通路：识别前一日弱势但尾盘未破位的样本
        # （chg ∈ [-2, 0.5], cp >= 0.50, turn >= 1.5, vr >= 0.9）。
        # 复盘 8/26 涨停票中此类样本约 10 只（顺钠股份/剑桥科技/立新能源
        # /恒银科技/锦龙股份/江西铜业等），这是 A 股常见的"低吸预备"形态。
        weak_setup_seed = bool(
            -2.0 <= change_pct <= 0.5
            and close_position >= 0.50
            and turnover >= 1.5
            and volume_ratio >= 0.9
        )
        if not (
            limit_probe
            or preheat_breakout
            or touch_board_pullback
            or quiet_setup_seed
            or mild_anomaly_seed
            or weak_setup_seed
            or washout_reversal
            or panic_flush
            or burst_pullback_ready
            or trend_preheat
            or momentum_shakeout_ready
            or strong_first_bearish_ready
            or board_memory_reset_ready
            or low_base_rotation_ready
        ):
            continue

        preheat_score = _clamp_score(
            _range_score(intraday_high_pct, ideal_low=5.5, ideal_high=9.4, min_value=1.5, max_value=12.0) * 0.28
            + _range_score(volume_ratio, ideal_low=1.1, ideal_high=3.8, min_value=0.5, max_value=7.0) * 0.22
            + _range_score(turnover, ideal_low=1.2, ideal_high=10.5, min_value=0.4, max_value=26.0) * 0.16
            + _range_score(close_position, ideal_low=0.52, ideal_high=0.92, min_value=0.15, max_value=1.0) * 0.18
            + _range_score(change_pct, ideal_low=1.0, ideal_high=6.8, min_value=-2.0, max_value=8.8) * 0.16
        )
        probe_wash_score = _clamp_score(
            50.0
            + _range_score(intraday_high_pct, ideal_low=8.0, ideal_high=10.2, min_value=5.5, max_value=12.0) * 0.18
            + _range_score(upper_gap_pct, ideal_low=3.2, ideal_high=8.5, min_value=1.0, max_value=14.0) * 0.18
            + _range_score(close_position, ideal_low=0.22, ideal_high=0.56, min_value=0.08, max_value=0.78) * 0.16
            + _range_score(volume_ratio, ideal_low=1.2, ideal_high=4.8, min_value=0.5, max_value=7.2) * 0.16
            + _range_score(turnover, ideal_low=1.2, ideal_high=11.5, min_value=0.4, max_value=24.0) * 0.12
            + _range_score(change_pct, ideal_low=-1.5, ideal_high=5.2, min_value=-5.5, max_value=8.8) * 0.12
            + _range_score(lower_gap_pct, ideal_low=0.5, ideal_high=3.8, min_value=0.0, max_value=8.0) * 0.08
        ) if touch_board_pullback else 0.0
        trend_preheat_score = _clamp_score(
            50.0
            + _range_score(volume_ratio, ideal_low=1.05, ideal_high=3.2, min_value=0.55, max_value=5.0) * 0.20
            + _range_score(change_pct, ideal_low=1.5, ideal_high=6.8, min_value=-1.0, max_value=8.8) * 0.18
            + _range_score(close_position, ideal_low=0.55, ideal_high=0.92, min_value=0.20, max_value=1.0) * 0.16
            + _range_score(_safe_percent_change(close_price, recent_reference_high), ideal_low=-1.5, ideal_high=1.8, min_value=-5.5, max_value=6.0) * 0.16
            + _range_score(turnover, ideal_low=1.0, ideal_high=10.5, min_value=0.4, max_value=24.0) * 0.14
            + _inverse_score(abs(_safe_percent_change(ma5, ma10)), good=1.2, bad=6.5) * 0.08
            + _range_score(_safe_percent_change(ma10, ma20) if ma20 > 0 else 0.0, ideal_low=-1.2, ideal_high=4.5, min_value=-8.0, max_value=10.0) * 0.08
        ) if trend_preheat else 0.0
        preheat_score = max(
            preheat_score,
            probe_wash_score,
            trend_preheat_score,
            _safe_float(burst_pullback_meta.get("burst_pullback_score")) if burst_pullback_ready else 0.0,
            _safe_float(momentum_shakeout_meta.get("momentum_shakeout_score")) if momentum_shakeout_ready else 0.0,
            strong_first_bearish_score,
            _safe_float(board_memory_meta.get("board_memory_reset_score")) if board_memory_reset_ready else 0.0,
            _safe_float(low_base_rotation_meta.get("low_base_rotation_score")) if low_base_rotation_ready else 0.0,
        )
        # 温和整理 + 强势收作为新的“蓄势预备”通路，给一个保守的初始分，
        # 后续通过 pre_board_probe_score 阈值过滤。该形态命中首板主要
        # 靠题材/板块扩散，单独形态分需控制在“观察池”上沿，不能直接
        # 进入正式预测榜，避免被题材降温日的样本噪声污染主榜。
        quiet_setup_score = (
            _clamp_score(
                46.0
                + _range_score(close_position, ideal_low=0.85, ideal_high=1.0, min_value=0.45, max_value=1.0) * 0.30
                + _inverse_score(amplitude, good=1.5, bad=5.5) * 0.22
                + _range_score(turnover, ideal_low=0.6, ideal_high=4.5, min_value=0.3, max_value=14.0) * 0.16
                + _range_score(volume_ratio, ideal_low=0.85, ideal_high=1.6, min_value=0.5, max_value=3.8) * 0.16
                + _range_score(_safe_percent_change(close_price, recent_reference_high), ideal_low=-3.0, ideal_high=1.5, min_value=-8.0, max_value=6.0) * 0.16
            ) if quiet_setup_seed else 0.0
        )
        if quiet_setup_seed and quiet_setup_score >= preheat_score:
            preheat_score = quiet_setup_score
            precursor_type = "quiet_setup"
        # 温和异动预备通路评分：以“强势收 + 正常换手”为主指标，避免和
        # 放量预热抢分。初始 48 分起步，介于 quiet_setup_score 与 preheat_score
        # 之间，最终还是要靠 pre_board_probe_score 阈值过滤。
        mild_anomaly_score = (
            _clamp_score(
                48.0
                + _range_score(change_pct, ideal_low=0.5, ideal_high=3.0, min_value=-1.0, max_value=6.0) * 0.24
                + _range_score(close_position, ideal_low=0.65, ideal_high=0.95, min_value=0.4, max_value=1.0) * 0.24
                + _range_score(turnover, ideal_low=1.5, ideal_high=8.0, min_value=0.6, max_value=20.0) * 0.20
                + _range_score(volume_ratio, ideal_low=0.7, ideal_high=2.2, min_value=0.4, max_value=5.0) * 0.18
                + _range_score(_safe_percent_change(close_price, recent_reference_high), ideal_low=-3.0, ideal_high=2.0, min_value=-8.0, max_value=6.0) * 0.14
            ) if mild_anomaly_seed else 0.0
        )
        if mild_anomaly_seed and mild_anomaly_score >= preheat_score:
            preheat_score = mild_anomaly_score
            precursor_type = "mild_anomaly_prep"
        # 弱势整理后反包预备通路评分：负涨幅+尾盘未破位+换手活跃，重点
        # 识别“主力在低位吸筹+等待催化”的样本。给较低初始分，必须通过
        # 板块/题材/资金确认才能升格。
        weak_setup_score = (
            _clamp_score(
                44.0
                + _range_score(close_position, ideal_low=0.50, ideal_high=0.85, min_value=0.30, max_value=1.0) * 0.28
                + _range_score(turnover, ideal_low=1.5, ideal_high=8.0, min_value=0.6, max_value=20.0) * 0.22
                + _range_score(volume_ratio, ideal_low=0.9, ideal_high=2.5, min_value=0.4, max_value=6.0) * 0.18
                + _inverse_score(abs(change_pct), good=0.3, bad=2.0) * 0.18
                + _range_score(_safe_percent_change(close_price, recent_reference_high), ideal_low=-3.0, ideal_high=1.5, min_value=-8.0, max_value=6.0) * 0.14
            ) if weak_setup_seed else 0.0
        )
        if weak_setup_seed and weak_setup_score >= preheat_score:
            preheat_score = weak_setup_score
            precursor_type = "weak_setup_rebound"
        oversold_score = _clamp_score(
            _range_score(abs(change_pct), ideal_low=2.8, ideal_high=8.5, min_value=1.2, max_value=10.5) * 0.28
            + _range_score(upper_gap_pct, ideal_low=2.2, ideal_high=10.0, min_value=0.8, max_value=16.0) * 0.24
            + _range_score(turnover, ideal_low=1.2, ideal_high=12.0, min_value=0.4, max_value=26.0) * 0.18
            + _range_score(amplitude, ideal_low=3.8, ideal_high=10.0, min_value=1.8, max_value=18.0) * 0.18
            + _inverse_score(lower_gap_pct, good=0.4, bad=4.5) * 0.12
        )
        precursor_type = "volume_preheat"
        if touch_board_pullback and probe_wash_score >= preheat_score - 0.1:
            precursor_type = "touch_board_pullback"
        elif strong_first_bearish_ready and strong_first_bearish_score >= preheat_score - 0.1:
            precursor_type = "strong_trend_deep_wash"
        elif momentum_shakeout_ready and _safe_float(momentum_shakeout_meta.get("momentum_shakeout_score")) >= preheat_score - 0.1:
            precursor_type = "momentum_shakeout"
        elif burst_pullback_ready and _safe_float(burst_pullback_meta.get("burst_pullback_score")) >= preheat_score - 0.1:
            precursor_type = "burst_pullback_restart"
        elif trend_preheat and trend_preheat_score >= preheat_score - 0.1:
            precursor_type = "short_ma_trend_preheat"
        elif board_memory_reset_ready and _safe_float(board_memory_meta.get("board_memory_reset_score")) >= preheat_score - 0.1:
            precursor_type = "board_memory_reset"
        elif low_base_rotation_ready and _safe_float(low_base_rotation_meta.get("low_base_rotation_score")) >= preheat_score - 0.1:
            precursor_type = "low_base_rotation"
        elif quiet_setup_seed and quiet_setup_score >= preheat_score - 0.1:
            precursor_type = "quiet_setup"
        elif mild_anomaly_seed and mild_anomaly_score >= preheat_score - 0.1:
            precursor_type = "mild_anomaly_prep"
        elif weak_setup_seed and weak_setup_score >= preheat_score - 0.1:
            precursor_type = "weak_setup_rebound"
        elif limit_probe:
            precursor_type = "hard_support_probe"
        if washout_reversal or panic_flush:
            precursor_type = "panic_repair"
        route = "pre_board_probe_start"
        event_type = "pre_board_probe"
        score = preheat_score
        if (washout_reversal or panic_flush) and oversold_score >= max(preheat_score - 4.0, OVERSOLD_REVERSAL_MIN_SCORE):
            route = "oversold_reversal_start"
            event_type = "oversold_reversal"
            score = oversold_score
        if strong_first_bearish_ready:
            # 收盘日只登记“深洗后待回收”，正式首板概率和买点仍由次日竞价、
            # 滚动60秒增量成交及板块/盘口确认产生，避免把放量长阴当抄底。
            route = "oversold_reversal_start"
            event_type = "oversold_reversal"
            precursor_type = "strong_trend_deep_wash"
            score = max(score, strong_first_bearish_score)
        if route == "pre_board_probe_start" and score < PRE_BOARD_PROBE_MIN_SCORE:
            # 形态种子(quiet_setup / mild_anomaly / weak_setup)走宽召回通路，
            # 分数天然偏低（44-58），用原阈值会直接过滤掉。让形态种子
            # 通过 score 过滤，进入观察池和主榜排序，再由 _is_actionable_
            # first_board_prediction 的概率阈值把控正式买点。
            if not (quiet_setup_seed or mild_anomaly_seed or weak_setup_seed):
                continue
        if route == "oversold_reversal_start" and score < OVERSOLD_REVERSAL_MIN_SCORE:
            continue

        support_strength = _clamp_score(
            42.0
            + score * 0.22
            + _range_score(turnover, ideal_low=1.2, ideal_high=10.5, min_value=0.4, max_value=26.0) * 0.12
            + (8.0 if limit_probe else 0.0)
            + (6.0 if preheat_breakout else 0.0)
            + (6.0 if washout_reversal or panic_flush else 0.0)
            + (7.0 if touch_board_pullback else 0.0)
            + (7.0 if burst_pullback_ready else 0.0)
            + (5.0 if trend_preheat else 0.0)
            + (5.0 if board_memory_reset_ready else 0.0)
            + (4.0 if low_base_rotation_ready else 0.0)
            + (3.5 if quiet_setup_seed else 0.0)
            + (3.0 if mild_anomaly_seed else 0.0)
            + (2.5 if weak_setup_seed else 0.0)
        )
        precursor_label_map = {
            "hard_support_probe": "临板强承接",
            "touch_board_pullback": "摸板回落蓄势",
            "volume_preheat": "放量突破预热",
            "short_ma_trend_preheat": "短均主升预热",
            "momentum_shakeout": "中期动量缩量洗盘",
            "burst_pullback_restart": "爆量深调再启动",
            "board_memory_reset": "历史涨停记忆缩量回撤",
            "low_base_rotation": "低位首波轮动预备",
            "panic_repair": "低位恐慌修复",
            "strong_trend_deep_wash": "主升首阴深洗回收预备",
            "quiet_setup": "温和整理蓄势",
            "mild_anomaly_prep": "温和异动预备",
            "weak_setup_rebound": "弱势整理反包预备",
        }
        candidates.append(
            (
                score,
                code,
                {
                    "code": code,
                    "name": stock_name,
                    "candidate_route": route,
                    "event_type": event_type,
                    "pre_board_probe_score": round(preheat_score, 2),
                    "oversold_reversal_score": round(oversold_score, 2),
                    "price": close_price,
                    "prev_close": prev_close,
                    "open": open_price,
                    "high": high_price,
                    "low": low_price,
                    "change_pct": round(change_pct, 2),
                    "intraday_high_pct": round(intraday_high_pct, 2),
                    "upper_gap_pct": round(upper_gap_pct, 2),
                    "lower_gap_pct": round(lower_gap_pct, 2),
                    "close_position": round(close_position, 3),
                    "amount": _safe_float(latest.get("amount")),
                    "volume": _safe_float(latest.get("volume")),
                    "volume_ratio": round(volume_ratio, 3),
                    "turnover": round(turnover, 3),
                    "amplitude": round(amplitude, 2),
                    "support_strength_score": round(support_strength, 2),
                    "limit_probe": limit_probe,
                    "preheat_breakout": preheat_breakout,
                    "touch_board_pullback": touch_board_pullback,
                    "trend_preheat": trend_preheat,
                    "washout_reversal": washout_reversal,
                    "market_washout_reversal_setup": market_washout_setup,
                    "market_washout_reversal_seed": market_washout_reversal_seed,
                    "panic_flush": panic_flush,
                    "quiet_setup_seed": quiet_setup_seed,
                    "quiet_setup_score": round(quiet_setup_score, 2),
                    "mild_anomaly_seed": mild_anomaly_seed,
                    "mild_anomaly_score": round(mild_anomaly_score, 2),
                    "weak_setup_seed": weak_setup_seed,
                    "weak_setup_score": round(weak_setup_score, 2),
                    "pre_board_precursor_type": precursor_type,
                    "pre_board_precursor_label": precursor_label_map.get(precursor_type, "涨停前兆"),
                    "momentum_shakeout_ready": momentum_shakeout_ready,
                    "momentum_shakeout_score": round(
                        _safe_float(momentum_shakeout_meta.get("momentum_shakeout_score")),
                        2,
                    ),
                    "momentum_shakeout_stats": momentum_shakeout_meta.get("momentum_shakeout_stats") or {},
                    "strong_first_bearish_ready": strong_first_bearish_ready,
                    "strong_first_bearish_score": round(strong_first_bearish_score, 2),
                    "strong_first_bearish_stats": strong_first_bearish_meta,
                    # 记忆回撤只是宽召回种子；没有竞价集群/硬消息/盘中增量
                    # 成交确认时，后续链路必须保持零仓位观察。
                    "prediction_shape_seed": bool(
                        momentum_shakeout_ready
                        or strong_first_bearish_ready
                        or board_memory_reset_ready
                        or low_base_rotation_ready
                        or market_washout_reversal_seed
                        or quiet_setup_seed
                        or mild_anomaly_seed
                        or weak_setup_seed
                    ),
                    "kline_close_reconciled_from_next_spot": kline_close_reconciled,
                    "board_memory_reset_only": bool(
                        board_memory_reset_ready
                        and not (
                            limit_probe
                            or preheat_breakout
                            or washout_reversal
                            or panic_flush
                            or burst_pullback_ready
                            or trend_preheat
                            or momentum_shakeout_ready
                            or strong_first_bearish_ready
                        )
                    ),
                    "low_base_rotation_only": bool(
                        low_base_rotation_ready
                        and not (
                            limit_probe
                            or preheat_breakout
                            or washout_reversal
                            or panic_flush
                            or burst_pullback_ready
                            or trend_preheat
                            or momentum_shakeout_ready
                            or strong_first_bearish_ready
                            or board_memory_reset_ready
                        )
                    ),
                    **board_memory_meta,
                    **low_base_rotation_meta,
                    **burst_pullback_meta,
                    "as_of": datetime.combine(trade_date, datetime.min.time()).isoformat(),
                },
            )
        )

    # 活跃试板/洗盘形态优先，宽召回的记忆回撤种子只占剩余名额；否则
    # 上百只低位相似K线会把盘中已点火的候选挤出900只下游富化池。
    candidates.sort(
        key=lambda item: (
            0 if bool(item[2].get("board_memory_reset_only") or item[2].get("low_base_rotation_only")) else 1,
            item[0],
        ),
        reverse=True,
    )
    effective_limit = _pre_board_probe_effective_limit(
        limit,
        market_washout_setup,
    )
    return {code: context for _score, code, context in candidates[:effective_limit]}


async def _load_quiet_setup_spots(
    db: AsyncSession,
    *,
    exclude_codes: set[str],
    limit: int = 1600,
) -> list[StockSpot]:
    stmt = select(StockSpot).where(
        StockSpot.price > 0,
        StockSpot.change_pct >= -1.5,
        StockSpot.change_pct <= 5.8,
        StockSpot.amplitude <= 5.8,
        StockSpot.volume_ratio >= 0.4,
        StockSpot.volume_ratio <= 1.9,
        StockSpot.turnover >= 0.5,
        StockSpot.turnover <= 12,
    )
    if exclude_codes:
        stmt = stmt.where(~StockSpot.code.in_(list(exclude_codes)))
    stmt = stmt.order_by(
        desc(StockSpot.support_strength_score),
        desc(StockSpot.turnover),
        desc(StockSpot.volume_ratio),
    ).limit(limit)
    result = await db.execute(stmt)
    return [
        item
        for item in result.scalars().all()
        if stock_tagger.is_tradeable(str(item.code or "").strip())
    ]


def _spot_updated_on_trade_date(spot: StockSpot, trade_date: date) -> bool:
    updated_at = getattr(spot, "updated_at", None)
    if updated_at is None:
        return True
    if isinstance(updated_at, datetime):
        return updated_at.date() == trade_date
    try:
        parsed = datetime.fromisoformat(str(updated_at).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    return parsed.date() == trade_date


def _select_spot_for_trade_date(
    current_spot: StockSpot | None,
    fallback_spot: StockSpot | None,
    trade_date: date,
) -> StockSpot | None:
    if current_spot is not None and _spot_updated_on_trade_date(current_spot, trade_date):
        return current_spot
    return fallback_spot or current_spot


async def _load_hot_mainline_relay_kline_spots(
    db: AsyncSession,
    codes: list[str],
    trade_date: date,
    *,
    limit: int,
    min_change_pct: float = HOT_MAINLINE_RELAY_MIN_CHANGE_PCT,
    min_volume_ratio: float = HOT_MAINLINE_RELAY_MIN_VOLUME_RATIO,
) -> list[StockSpot]:
    if not codes:
        return []

    start_date = trade_date - timedelta(days=30)
    kline_result = await db.execute(
        select(
            StockKline.code,
            StockKline.trade_date,
            StockKline.open,
            StockKline.close,
            StockKline.high,
            StockKline.low,
            StockKline.volume,
            StockKline.amount,
            StockKline.turnover,
            StockKline.change_pct,
            StockKline.prev_close,
        )
        .where(
            StockKline.code.in_(codes),
            StockKline.trade_date <= trade_date,
            StockKline.trade_date >= start_date,
        )
        .order_by(StockKline.code, StockKline.trade_date)
    )
    grouped: dict[str, list[dict]] = {}
    for row in kline_result.all():
        grouped.setdefault(str(row[0] or ""), []).append(
            {
                "trade_date": row[1],
                "open": _safe_float(row[2]),
                "close": _safe_float(row[3]),
                "high": _safe_float(row[4]),
                "low": _safe_float(row[5]),
                "volume": _safe_float(row[6]),
                "amount": _safe_float(row[7]),
                "turnover": _safe_float(row[8]),
                "change_pct": _safe_float(row[9]),
                "prev_close": _safe_float(row[10]),
            }
        )

    fund_flow_map = await _load_fund_flow_map(db, codes, trade_date)
    name_result = await db.execute(select(StockSpot.code, StockSpot.name).where(StockSpot.code.in_(codes)))
    name_map = {str(code or ""): str(name or "") for code, name in name_result.all()}

    pseudo_spots: list[StockSpot] = []
    for code, bars in grouped.items():
        if not bars or bars[-1].get("trade_date") != trade_date:
            continue
        latest = bars[-1]
        previous_volumes = [_safe_float(item.get("volume")) for item in bars[-6:-1] if _safe_float(item.get("volume")) > 0]
        avg_previous_volume = _mean(previous_volumes)
        volume_ratio = _safe_float(latest.get("volume")) / avg_previous_volume if avg_previous_volume > 0 else 0.0
        change_pct = _safe_float(latest.get("change_pct"))
        if change_pct == 0 and _safe_float(latest.get("prev_close")) > 0 and _safe_float(latest.get("close")) > 0:
            change_pct = _safe_percent_change(_safe_float(latest.get("close")), _safe_float(latest.get("prev_close")))
        turnover = _safe_float(latest.get("turnover"))
        if (
            change_pct < min_change_pct
            or change_pct > HOT_MAINLINE_RELAY_MAX_CHANGE_PCT
            or volume_ratio < min_volume_ratio
            or turnover < 0.3
            or turnover > HOT_MAINLINE_RELAY_MAX_TURNOVER
        ):
            continue

        fund_flow = fund_flow_map.get(code)
        main_net_inflow = _safe_float(getattr(fund_flow, "main_net_inflow", 0.0))
        main_net_inflow_pct = _safe_float(getattr(fund_flow, "main_net_inflow_pct", 0.0))
        if fund_flow is None:
            main_net_inflow_pct = 0.0
        support_strength = _clamp_score(50.0 + min(max(main_net_inflow_pct, -8.0), 16.0) * 1.2 + change_pct * 1.4)
        pseudo_spots.append(
            StockSpot(
                code=code,
                name=name_map.get(code) or code,
                price=_safe_float(latest.get("close")),
                prev_close=_safe_float(latest.get("prev_close")),
                open=_safe_float(latest.get("open")),
                high=_safe_float(latest.get("high")),
                low=_safe_float(latest.get("low")),
                change_pct=round(change_pct, 2),
                volume=int(_safe_float(latest.get("volume"))),
                amount=_safe_float(latest.get("amount")),
                turnover=turnover,
                volume_ratio=round(volume_ratio, 3),
                main_net_inflow=main_net_inflow,
                support_strength_score=support_strength,
                updated_at=datetime.combine(trade_date, datetime.min.time()),
            )
        )

    pseudo_spots.sort(
        key=lambda item: (
            _safe_float(item.change_pct),
            _safe_float(item.volume_ratio),
            _safe_float(item.support_strength_score),
            _safe_float(item.turnover),
        ),
        reverse=True,
    )
    return pseudo_spots[:limit]


async def _load_hot_mainline_relay_spots(
    db: AsyncSession,
    trade_date: date,
    *,
    exclude_codes: set[str],
    market_context: dict | None = None,
    limit: int = HOT_MAINLINE_RELAY_SCAN_LIMIT,
) -> list[StockSpot]:
    sector_result = await db.execute(
        select(
            SectorPersistence.sector_code,
            SectorPersistence.sector_name,
            SectorPersistence.strength_score,
            SectorPersistence.limit_up_count,
            SectorPersistence.fund_flow,
            SectorPersistence.change_pct,
            SectorPersistence.consecutive_days,
            SectorInfo.is_excluded,
            SectorInfo.stock_count,
        )
        .outerjoin(SectorInfo, SectorPersistence.sector_code == SectorInfo.sector_code)
        .where(SectorPersistence.trade_date == trade_date)
    )
    hot_sector_codes: list[str] = []
    for row in sector_result.all():
        sector_code = str(row[0] or "").strip()
        sector_name = str(row[1] or "").strip()
        strength_score = _safe_float(row[2])
        limit_up_count = _safe_int(row[3])
        fund_flow = _safe_float(row[4])
        change_pct = _safe_float(row[5])
        consecutive_days = _safe_int(row[6])
        is_excluded = bool(_safe_int(row[7]))
        sector_stock_count = _safe_int(row[8])
        if not sector_code or is_excluded or sector_name in GENERIC_RELATIONSHIP_SECTOR_NAMES:
            continue
        is_hot_mainline = (
            strength_score >= HOT_MAINLINE_RELAY_MIN_SECTOR_STRENGTH
            and limit_up_count >= HOT_MAINLINE_RELAY_MIN_LIMIT_UP_COUNT
        )
        has_money_and_continuity = fund_flow >= 3.0 and change_pct >= 0.8 and consecutive_days >= 2
        is_broad_rotation_sector = (
            bool((market_context or {}).get("broad_first_board_overflow"))
            and limit_up_count >= 1
            and (
                (
                    strength_score >= 45.0
                    and change_pct >= 0.0
                    and (fund_flow >= 0.0 or limit_up_count >= 2 or consecutive_days <= 3)
                )
                or _is_broad_rotation_cluster_sector(
                    {
                        "strength_score": strength_score,
                        "limit_up_count": limit_up_count,
                        "fund_flow": fund_flow,
                        "change_pct": change_pct,
                        "consecutive_days": consecutive_days,
                    },
                    market_context,
                )
            )
        )
        if is_hot_mainline or has_money_and_continuity or is_broad_rotation_sector:
            hot_sector_codes.append(sector_code)

    # 板块派生通常需要几十秒到两分钟。直接用当日主板涨停成员宽度补一条
    # 快速路径，使冰点修复日的新主线不必等 consecutive_days 到第二天。
    live_limit_up_map = (
        await _load_live_main_board_sector_limit_up_context(db, trade_date)
        if trade_date == date.today()
        else {}
    )
    hot_sector_codes.extend(
        sector_code
        for sector_code, item in live_limit_up_map.items()
        if _safe_int(item.get("limit_up_count")) >= HOT_MAINLINE_RELAY_MIN_LIMIT_UP_COUNT
    )
    hot_sector_codes = list(dict.fromkeys(hot_sector_codes))

    if not hot_sector_codes:
        return []

    mapping_result = await db.execute(
        select(StockSectorMapping.code)
        .where(
            StockSectorMapping.sector_code.in_(hot_sector_codes),
            StockSectorMapping.sector_type.in_(ALLOWED_MAINLINE_SECTOR_TYPES),
        )
    )
    codes = [
        code
        for code in dict.fromkeys(str(row[0] or "").strip() for row in mapping_result.all())
        if code and code not in exclude_codes and stock_tagger.is_tradeable(code)
    ]
    if not codes:
        return []

    broad_rotation_scan = bool((market_context or {}).get("broad_first_board_overflow"))
    min_change_pct = -6.5 if broad_rotation_scan else HOT_MAINLINE_RELAY_MIN_CHANGE_PCT
    min_volume_ratio = 0.4 if broad_rotation_scan else HOT_MAINLINE_RELAY_MIN_VOLUME_RATIO
    stmt = (
        select(StockSpot)
        .where(
            StockSpot.code.in_(codes),
            StockSpot.price > 0,
            StockSpot.change_pct >= min_change_pct,
            StockSpot.change_pct <= HOT_MAINLINE_RELAY_MAX_CHANGE_PCT,
            StockSpot.volume_ratio >= min_volume_ratio,
            StockSpot.turnover >= 0.3,
            StockSpot.turnover <= HOT_MAINLINE_RELAY_MAX_TURNOVER,
        )
        .order_by(
            desc(StockSpot.change_pct),
            desc(StockSpot.volume_ratio),
            desc(StockSpot.support_strength_score),
            desc(StockSpot.turnover),
        )
        .limit(limit)
    )
    result = await db.execute(stmt)
    realtime_spots = [
        spot
        for spot in result.scalars().all()
        if _spot_updated_on_trade_date(spot, trade_date)
    ]
    if realtime_spots:
        return realtime_spots
    return await _load_hot_mainline_relay_kline_spots(
        db,
        codes,
        trade_date,
        limit=limit,
        min_change_pct=min_change_pct,
        min_volume_ratio=min_volume_ratio,
    )


def _prioritize_washout_rotation_spots(
    spots: list[StockSpot],
    *,
    washout_sector_by_code: dict[str, list[str]],
    rotation_map: dict[str, dict],
    limit: int,
) -> list[StockSpot]:
    """给每个洗盘主线保留最小样本配额，避免宽泛大板块挤满全局TopN。"""
    if limit <= 0 or not spots:
        return []
    if not washout_sector_by_code:
        return spots[:limit]

    by_code = {str(spot.code or "").strip(): spot for spot in spots if str(spot.code or "").strip()}
    by_sector: dict[str, list[StockSpot]] = defaultdict(list)
    for code, sector_codes in washout_sector_by_code.items():
        spot = by_code.get(code)
        if spot is None:
            continue
        for sector_code in sector_codes:
            by_sector[sector_code].append(spot)

    sector_order = sorted(
        by_sector,
        key=lambda sector_code: (
            _safe_float((rotation_map.get(sector_code) or {}).get("sector_washout_score")),
            _safe_int((rotation_map.get(sector_code) or {}).get("sector_current_limit_up_count")),
            _safe_int((rotation_map.get(sector_code) or {}).get("sector_recent_limit_up_sum")),
        ),
        reverse=True,
    )
    if not sector_order:
        return spots[:limit]

    reserved_limit = min(limit, max(round(limit * 0.35), len(sector_order) * 6))
    quota_per_sector = max(6, min(14, reserved_limit // len(sector_order)))

    def member_rank(spot: StockSpot) -> tuple:
        change_pct = _safe_float(spot.change_pct)
        volume_ratio = _safe_float(spot.volume_ratio)
        return (
            _range_score(
                change_pct,
                ideal_low=-6.5,
                ideal_high=-1.0,
                min_value=SECTOR_WASHOUT_WATCH_MIN_CHANGE_PCT,
                max_value=2.0,
            ),
            _range_score(volume_ratio, ideal_low=0.55, ideal_high=2.8, min_value=0.25, max_value=8.0),
            _safe_float(spot.support_strength_score),
            _safe_float(spot.turnover),
        )

    selected: list[StockSpot] = []
    selected_codes: set[str] = set()
    for sector_code in sector_order:
        sector_spots = sorted(by_sector[sector_code], key=member_rank, reverse=True)
        added_for_sector = 0
        for spot in sector_spots:
            code = str(spot.code or "").strip()
            if not code or code in selected_codes:
                continue
            selected.append(spot)
            selected_codes.add(code)
            setattr(spot, "_promotion_washout_reserved", True)
            added_for_sector += 1
            if len(selected) >= reserved_limit or added_for_sector >= quota_per_sector:
                break
        if len(selected) >= reserved_limit:
            break

    for spot in spots:
        code = str(spot.code or "").strip()
        if not code or code in selected_codes:
            continue
        selected.append(spot)
        selected_codes.add(code)
        if len(selected) >= limit:
            break
    return selected[:limit]


async def _load_broad_rotation_first_board_spots(
    db: AsyncSession,
    trade_date: date,
    *,
    exclude_codes: set[str],
    market_context: dict | None = None,
    limit: int = BROAD_ROTATION_FIRST_BOARD_SCAN_LIMIT,
) -> list[StockSpot]:
    sector_result = await db.execute(
        select(
            SectorPersistence.sector_code,
            SectorPersistence.sector_name,
            SectorPersistence.strength_score,
            SectorPersistence.limit_up_count,
            SectorPersistence.fund_flow,
            SectorPersistence.change_pct,
            SectorPersistence.consecutive_days,
            SectorInfo.is_excluded,
            SectorInfo.stock_count,
        )
        .outerjoin(SectorInfo, SectorPersistence.sector_code == SectorInfo.sector_code)
        .where(SectorPersistence.trade_date == trade_date)
    )
    rows = sector_result.all()
    sector_codes = [str(row[0] or "").strip() for row in rows if str(row[0] or "").strip()]
    rotation_map = await _load_sector_rotation_context(db, trade_date, sector_codes)

    selected_sector_codes: list[str] = []
    for row in rows:
        sector_code = str(row[0] or "").strip()
        sector_name = str(row[1] or "").strip()
        strength_score = _safe_float(row[2])
        limit_up_count = _safe_int(row[3])
        fund_flow = _safe_float(row[4])
        change_pct = _safe_float(row[5])
        consecutive_days = _safe_int(row[6])
        is_excluded = bool(_safe_int(row[7]))
        sector_stock_count = _safe_int(row[8])
        if not sector_code or is_excluded or sector_name in GENERIC_RELATIONSHIP_SECTOR_NAMES:
            continue
        rotation = rotation_map.get(sector_code, {})
        rotation_score = _safe_float(rotation.get("sector_rotation_score"))
        strength_delta = _safe_float(rotation.get("sector_strength_delta"))
        limit_up_delta = _safe_float(rotation.get("sector_limit_up_delta"))
        low_position_rotation = bool(rotation.get("sector_low_position_rotation"))
        washout_reversal_watch = bool(
            rotation.get("sector_washout_reversal_watch")
            and (sector_stock_count <= 0 or sector_stock_count <= SECTOR_WASHOUT_WATCH_MAX_MEMBER_COUNT)
        )
        cluster_sector = _is_broad_rotation_cluster_sector(
            {
                "strength_score": strength_score,
                "limit_up_count": limit_up_count,
                "fund_flow": fund_flow,
                "change_pct": change_pct,
                "consecutive_days": consecutive_days,
                **rotation,
            },
            market_context,
        )
        sector_catalyst = bool(
            (
                limit_up_count >= 2
                and (
                    strength_score >= BROAD_ROTATION_FIRST_BOARD_MIN_SECTOR_STRENGTH
                    or rotation_score >= BROAD_ROTATION_FIRST_BOARD_MIN_ROTATION_SCORE
                    or strength_delta >= 8.0
                    or limit_up_delta >= 1.5
                    or low_position_rotation
                )
            )
            or (
                strength_score >= MAINLINE_SPREAD_MIN_SECTOR_STRENGTH
                and limit_up_count >= 1
                and change_pct >= -1.0
            )
            or washout_reversal_watch
            or cluster_sector
        )
        if sector_catalyst:
            selected_sector_codes.append(sector_code)

    if not selected_sector_codes:
        return []

    mapping_result = await db.execute(
        select(StockSectorMapping.code, StockSectorMapping.sector_code)
        .where(
            StockSectorMapping.sector_code.in_(selected_sector_codes),
            StockSectorMapping.sector_type.in_(ALLOWED_MAINLINE_SECTOR_TYPES),
        )
    )
    mapping_rows = mapping_result.all()
    codes = [
        code
        for code in dict.fromkeys(str(row[0] or "").strip() for row in mapping_rows)
        if code and code not in exclude_codes and stock_tagger.is_tradeable(code)
    ]
    if not codes:
        return []

    has_washout_sector = any(
        bool((rotation_map.get(sector_code) or {}).get("sector_washout_reversal_watch"))
        for sector_code in selected_sector_codes
    )
    scan_min_change_pct = (
        SECTOR_WASHOUT_WATCH_MIN_CHANGE_PCT
        if has_washout_sector
        else BROAD_ROTATION_FIRST_BOARD_MIN_CHANGE_PCT
    )
    washout_sector_codes = {
        sector_code
        for sector_code in selected_sector_codes
        if bool((rotation_map.get(sector_code) or {}).get("sector_washout_reversal_watch"))
    }
    washout_sector_by_code: dict[str, list[str]] = defaultdict(list)
    for raw_code, raw_sector_code in mapping_rows:
        code = str(raw_code or "").strip()
        sector_code = str(raw_sector_code or "").strip()
        if code and code in codes and sector_code in washout_sector_codes:
            washout_sector_by_code[code].append(sector_code)
    fetch_limit = min(max(limit * 3, 1800), 3000) if has_washout_sector else limit
    stmt = (
        select(StockSpot)
        .where(
            StockSpot.code.in_(codes),
            StockSpot.price > 0,
            StockSpot.change_pct >= scan_min_change_pct,
            StockSpot.change_pct <= MAINLINE_SPREAD_MAX_CHANGE_PCT,
            StockSpot.volume_ratio >= BROAD_ROTATION_FIRST_BOARD_MIN_VOLUME_RATIO,
            StockSpot.turnover >= 0.25,
            StockSpot.turnover <= HOT_MAINLINE_RELAY_MAX_TURNOVER,
        )
        .order_by(
            desc(StockSpot.support_strength_score),
            desc(StockSpot.volume_ratio),
            desc(StockSpot.change_pct),
            desc(StockSpot.turnover),
        )
        .limit(fetch_limit)
    )
    result = await db.execute(stmt)
    realtime_spots = [
        spot
        for spot in result.scalars().all()
        if _spot_updated_on_trade_date(spot, trade_date)
    ]
    if realtime_spots:
        return _prioritize_washout_rotation_spots(
            realtime_spots,
            washout_sector_by_code=washout_sector_by_code,
            rotation_map=rotation_map,
            limit=limit,
        )
    fallback_spots = await _load_hot_mainline_relay_kline_spots(
        db,
        codes,
        trade_date,
        limit=fetch_limit,
        min_change_pct=scan_min_change_pct,
        min_volume_ratio=BROAD_ROTATION_FIRST_BOARD_MIN_VOLUME_RATIO,
    )
    return _prioritize_washout_rotation_spots(
        fallback_spots,
        washout_sector_by_code=washout_sector_by_code,
        rotation_map=rotation_map,
        limit=limit,
    )


async def _load_first_board_kline_context(
    db: AsyncSession,
    codes: list[str],
    trade_date: date,
    *,
    lookback_days: int = 420,
    as_of_date: date | None = None,
    session_name: str | None = None,
) -> dict[str, dict]:
    if not codes:
        return {}

    start_date = trade_date - timedelta(days=lookback_days)
    result = await db.execute(
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
            StockKline.source,
        ).where(
            StockKline.code.in_(codes),
            StockKline.trade_date <= trade_date,
            StockKline.trade_date >= start_date,
        ).order_by(StockKline.code, StockKline.trade_date)
    )

    current_session = session_name or trade_calendar.get_trade_session()
    intraday_sessions = {"morning", "afternoon"}
    current_trade_date = as_of_date or trade_date
    grouped: dict[str, list[dict]] = {}
    for (
        code,
        trade_day,
        open_price,
        close_price,
        high_price,
        low_price,
        volume,
        turnover,
        change_pct,
        source,
    ) in result.all():
        is_provisional = (
            str(source or "") == "spot_fallback"
            and trade_day == trade_date
            and trade_day == current_trade_date
            and current_session in intraday_sessions
        )
        grouped.setdefault(str(code or ""), []).append(
            {
                "trade_date": trade_day,
                "open": _safe_float(open_price),
                "close": _safe_float(close_price),
                "high": _safe_float(high_price),
                "low": _safe_float(low_price),
                "volume": _safe_float(volume),
                "turnover": _safe_float(turnover),
                "change_pct": _safe_float(change_pct),
                "source": str(source or ""),
                "is_provisional": is_provisional,
            }
        )

    return {
        code: _build_first_board_kline_confirmation(bars[-260:])
        for code, bars in grouped.items()
        if code
    }


async def _load_recent_limit_up_memory(
    db: AsyncSession,
    codes: list[str],
    trade_date: date,
    *,
    lookback_days: int = 120,
) -> dict[str, dict]:
    if not codes:
        return {}

    start_date = trade_date - timedelta(days=lookback_days)
    kline_result = await db.execute(
        select(
            StockKline.code,
            StockKline.trade_date,
            StockKline.change_pct,
            StockKline.prev_close,
            StockKline.close,
            StockKline.high,
        ).where(
            StockKline.code.in_(codes),
            StockKline.trade_date <= trade_date,
            StockKline.trade_date >= start_date,
        )
    )
    kline_map = {
        (str(code or ""), kline_trade_date): {
            "change_pct": _safe_float(change_pct),
            "prev_close": _safe_float(prev_close),
            "close": _safe_float(close),
            "high": _safe_float(high_price),
        }
        for code, kline_trade_date, change_pct, prev_close, close, high_price in kline_result.all()
    }
    kline_coverage_count = {}
    for code, _kline_trade_date in kline_map.keys():
        normalized_code = str(code or "")
        if not normalized_code:
            continue
        kline_coverage_count[normalized_code] = kline_coverage_count.get(normalized_code, 0) + 1

    result = await db.execute(
        select(
            LimitUpPool.code,
            LimitUpPool.name,
            LimitUpPool.trade_date,
            LimitUpPool.consecutive_days,
            LimitUpPool.seal_amount,
            LimitUpPool.break_count,
        ).where(
            LimitUpPool.code.in_(codes),
            LimitUpPool.trade_date <= trade_date,
            LimitUpPool.trade_date >= start_date,
        ).order_by(LimitUpPool.code, desc(LimitUpPool.trade_date))
    )

    grouped: dict[str, list[dict]] = {}
    for code, name, limit_up_trade_date, consecutive_days, seal_amount, break_count in result.all():
        grouped.setdefault(str(code or ""), []).append(
            {
                "name": str(name or ""),
                "trade_date": limit_up_trade_date,
                "consecutive_days": _safe_int(consecutive_days, 1),
                "seal_amount": _safe_float(seal_amount),
                "break_count": _safe_int(break_count),
            }
        )

    day_gap_cache: dict[date, int] = {}
    features_by_code: dict[str, dict] = {}
    for code in codes:
        raw_entries = grouped.get(code, [])
        validated_entries: list[dict] = []
        invalid_hits = 0
        missing_kline_hits = 0
        for entry in raw_entries:
            kline = kline_map.get((code, entry["trade_date"]))
            if kline is None:
                if kline_coverage_count.get(code, 0) > 0:
                    invalid_hits += 1
                    missing_kline_hits += 1
                    continue
                validated_entries.append(entry)
                continue

            board_type = stock_tagger.get_board_type(code)
            name = str(entry.get("name") or "")
            if "ST" in name.upper():
                threshold = 4.7
            elif board_type in {"gem", "star"}:
                threshold = 19.0
            elif board_type == "bse":
                threshold = 29.0
            else:
                threshold = 9.5

            actual_change_pct = _safe_float(kline.get("change_pct"))
            if actual_change_pct <= 0 and _safe_float(kline.get("prev_close")) > 0 and _safe_float(kline.get("close")) > 0:
                actual_change_pct = _safe_percent_change(_safe_float(kline.get("close")), _safe_float(kline.get("prev_close")))

            if actual_change_pct >= threshold:
                validated_entries.append(entry)
            else:
                invalid_hits += 1

        entries = validated_entries
        if not entries:
            features_by_code[code] = {
                "memory_score": 0.0,
                "memory_max_board_50d": 0,
                "memory_limit_up_hits_50d": 0,
                "memory_raw_limit_up_hits_50d": len(raw_entries),
                "memory_invalid_limit_up_hits_50d": invalid_hits,
                "memory_missing_kline_limit_up_hits_50d": missing_kline_hits,
                "memory_days_since_last_limit_up": 999,
                "memory_last_limit_up_date": None,
                "memory_last_seal_amount": 0.0,
                "memory_last_break_count": 0,
                "memory_last_consecutive_days": 0,
                "memory_last_limit_up_close": 0.0,
                "memory_last_limit_up_high": 0.0,
                "memory_last_limit_up_anchor_price": 0.0,
                "memory_validation_mode": "stock_kline.change_pct+missing_kline_filter",
            }
            continue

        latest_entry = entries[0]
        latest_date = latest_entry["trade_date"]
        latest_kline = kline_map.get((code, latest_date), {})
        if latest_date not in day_gap_cache:
            day_gap_cache[latest_date] = await _trade_day_gap(db, latest_date, trade_date)
        days_since_last = day_gap_cache[latest_date]
        max_board = max(_safe_int(item.get("consecutive_days"), 1) for item in entries)
        limit_up_hits = len(entries)
        last_seal_amount = _safe_float(latest_entry.get("seal_amount"))
        last_break_count = _safe_int(latest_entry.get("break_count"))

        if 6 <= days_since_last <= 25:
            recency_score = 100.0
        elif days_since_last < 6:
            recency_score = 40.0 + max(days_since_last, 0) / 6.0 * 35.0
        elif days_since_last <= 45:
            recency_score = 100.0 - (days_since_last - 25) / 20.0 * 65.0
        else:
            recency_score = max(10.0, 35.0 - min(days_since_last - 45, 25) * 1.0)

        memory_score = (
            min(max_board, 4) / 4.0 * 25.0
            + min(limit_up_hits, 3) / 3.0 * 15.0
            + recency_score * 0.20
            + min(last_seal_amount / 3e8, 1.0) * 20.0
            + max(0.0, 1.0 - min(last_break_count, 3) / 3.0) * 20.0
        )
        features_by_code[code] = {
            "memory_score": _clamp_score(memory_score),
            "memory_max_board_50d": max_board,
            "memory_limit_up_hits_50d": limit_up_hits,
            "memory_raw_limit_up_hits_50d": len(raw_entries),
            "memory_invalid_limit_up_hits_50d": invalid_hits,
            "memory_missing_kline_limit_up_hits_50d": missing_kline_hits,
            "memory_days_since_last_limit_up": days_since_last,
            "memory_last_limit_up_date": str(latest_date) if latest_date else None,
            "memory_last_seal_amount": round(last_seal_amount, 2),
            "memory_last_break_count": last_break_count,
            "memory_last_consecutive_days": _safe_int(latest_entry.get("consecutive_days"), 1),
            "memory_last_limit_up_close": round(_safe_float(latest_kline.get("close")), 2),
            "memory_last_limit_up_high": round(_safe_float(latest_kline.get("high")), 2),
            "memory_last_limit_up_anchor_price": round(
                max(_safe_float(latest_kline.get("high")), _safe_float(latest_kline.get("close"))),
                2,
            ),
            "memory_validation_mode": "stock_kline.change_pct+missing_kline_filter",
        }

    return features_by_code


def _build_quiet_setup_row(
    spot: StockSpot,
    bull_row: dict,
    kline_context: dict,
    fund_flow: FundFlow | None = None,
) -> dict:
    main_net_inflow, main_net_inflow_pct = _flow_value_from_spot_or_fund(spot, fund_flow)
    bull_score = _safe_float(bull_row.get("total_score"))
    support_strength = _safe_float(spot.support_strength_score)
    kline_score = _safe_float(kline_context.get("confirmation_score"))
    setup_grade = (
        "A2 盘口确认后执行"
        if bull_score >= 84 and support_strength >= 66 and kline_score >= 0.7
        else "B类观察候选"
    )
    top_signals = bull_row.get("top_signals") or []
    detail = {
        "price": _safe_float(spot.price),
        "prev_close": _safe_float(spot.prev_close),
        "open": _safe_float(spot.open),
        "high": _safe_float(spot.high),
        "low": _safe_float(spot.low),
        "change_pct": _safe_float(spot.change_pct),
        "main_net_inflow": main_net_inflow,
        "main_net_inflow_pct": main_net_inflow_pct,
        "main_fund_status": "unknown" if main_net_inflow is None or main_net_inflow_pct is None else "dated_fund_flow",
        "amount": _safe_float(spot.amount),
        "volume_ratio": _safe_float(spot.volume_ratio),
        "support_strength_score": support_strength,
        "turnover": _safe_float(spot.turnover),
        "amplitude": _safe_float(spot.amplitude),
        "as_of": spot.updated_at.isoformat() if getattr(spot, "updated_at", None) else "",
    }
    secondary_parts = [str(kline_context.get("summary") or "").strip()]
    if top_signals:
        secondary_parts.append(" / ".join(str(item or "").strip() for item in top_signals[:2] if str(item or "").strip()))
    return {
        "code": str(spot.code or "").strip(),
        "name": str(spot.name or "").strip(),
        "event_types": ["stealth_setup"],
        "setup_grade": setup_grade,
        "setup_grade_display": setup_grade,
        "display_score": round(max(bull_score, 60.0) + kline_score * 18, 2),
        "detail": detail,
        "risk_flags": [],
        "a1_blockers": [],
        "primary_reason": "静默蓄势待发",
        "secondary_reason": " · ".join(part for part in secondary_parts if part),
        "latest_as_of": detail["as_of"],
    }


def _build_quiet_setup_bull_row(
    spot: StockSpot,
    sector: dict,
    kline_context: dict,
    existing_row: dict | None = None,
) -> dict:
    if existing_row:
        return existing_row

    support_strength = _safe_float(spot.support_strength_score)
    sector_strength = _safe_float(sector.get("strength_score"))
    kline_score = _safe_float(kline_context.get("confirmation_score")) * 100.0
    volume_ratio = _safe_float(spot.volume_ratio)
    change_pct = _safe_float(spot.change_pct)

    volume_fit = 12.0 if 0.6 <= volume_ratio <= 1.4 else 7.0 if 0.4 <= volume_ratio <= 1.8 else 0.0
    change_fit = 10.0 if -0.5 <= change_pct <= 3.5 else 6.0 if -1.5 <= change_pct <= 5.8 else 0.0
    total_score = min(
        88.0,
        round(
            support_strength * 0.28
            + sector_strength * 0.18
            + kline_score * 0.34
            + volume_fit
            + change_fit,
            2,
        ),
    )
    level = "A" if total_score >= 75 else "B" if total_score >= 62 else "C"
    top_signals: list[str] = []
    if bool(kline_context.get("qualified_doji_confirmation")):
        top_signals.append("十字星蓄势")
    if bool(kline_context.get("has_platform_contraction")):
        top_signals.append("平台压缩")
    if bool(kline_context.get("has_volume_contraction")):
        top_signals.append("缩量蓄势")
    if bool(kline_context.get("near_breakout")):
        top_signals.append("贴近前高")
    return {
        "code": str(spot.code or "").strip(),
        "name": str(spot.name or "").strip(),
        "total_score": total_score,
        "level": level,
        "top_signals": top_signals,
    }


def _flow_value_from_spot_or_fund(
    spot: StockSpot | None, fund_flow: FundFlow | None = None,
) -> tuple[float | None, float | None]:
    """Compatibility name only: spot is NOT a fund source.

    Prediction callers may pass a dated historical FundFlow for research; that
    does not certify current execution freshness. Missing amount/pct remain
    unknown, and pct is never estimated from a different spot's turnover.
    """
    values = []
    for key in ("main_net_inflow", "main_net_inflow_pct"):
        value = getattr(fund_flow, key, None) if fund_flow is not None else None
        try:
            values.append(float(value) if not isinstance(value, bool)
                          and isfinite(float(value)) else None)
        except (TypeError, ValueError, OverflowError):
            values.append(None)
    return values[0], values[1]


def _fund_range_score(value, **bounds) -> float:
    # Unknown funds earn no confirmation score; measured zero is still measured.
    return _range_score(value, **bounds) if value is not None else 0.0


def _build_hot_mainline_relay_bull_row(
    spot: StockSpot,
    sector: dict,
    kline_context: dict,
    fund_flow: FundFlow | None = None,
    existing_row: dict | None = None,
) -> dict:
    if existing_row:
        return existing_row

    support_strength = _safe_float(spot.support_strength_score)
    sector_strength = _safe_float(sector.get("strength_score"))
    kline_score = _safe_float(kline_context.get("confirmation_score")) * 100.0
    change_pct = _safe_float(spot.change_pct)
    volume_ratio = _safe_float(spot.volume_ratio)
    _main_net_inflow, main_net_inflow_pct = _flow_value_from_spot_or_fund(spot, fund_flow)
    trend_bonus = (
        10.0
        if bool(kline_context.get("trend_acceleration_ready"))
        else 6.0
        if bool(kline_context.get("trend_breakout"))
        else 3.0
        if bool(kline_context.get("near_trend_high")) and bool(kline_context.get("ma_alignment"))
        else 0.0
    )
    volume_fit = _range_score(volume_ratio, ideal_low=1.1, ideal_high=2.8, min_value=0.6, max_value=4.5) * 0.12
    change_fit = _range_score(change_pct, ideal_low=2.0, ideal_high=6.8, min_value=0.3, max_value=9.0) * 0.10
    flow_fit = _fund_range_score(main_net_inflow_pct, ideal_low=4.0, ideal_high=14.0, min_value=-3.0, max_value=20.0) * 0.12
    total_score = min(
        92.0,
        round(
            support_strength * 0.20
            + sector_strength * 0.28
            + kline_score * 0.18
            + volume_fit
            + change_fit
            + flow_fit
            + trend_bonus,
            2,
        ),
    )
    level = "A" if total_score >= 78 else "B" if total_score >= 64 else "C"
    top_signals: list[str] = []
    if bool(kline_context.get("trend_acceleration_ready")):
        top_signals.append("趋势新高加速")
    elif bool(kline_context.get("trend_breakout")):
        top_signals.append("趋势突破预备")
    elif bool(kline_context.get("near_trend_high")) and bool(kline_context.get("ma_alignment")):
        top_signals.append("贴近趋势前高")
    if sector_strength >= HOT_MAINLINE_RELAY_MIN_SECTOR_STRENGTH:
        top_signals.append("热门主线补涨")
    if _safe_float(main_net_inflow_pct) >= 4.0:
        top_signals.append("主力流入确认")
    return {
        "code": str(spot.code or "").strip(),
        "name": str(spot.name or "").strip(),
        "total_score": total_score,
        "level": level,
        "top_signals": top_signals,
    }


def _build_hot_mainline_relay_row(
    spot: StockSpot,
    *,
    sector: dict,
    bull_row: dict,
    kline_context: dict,
    fund_flow: FundFlow | None = None,
    market_context: dict | None = None,
) -> dict:
    main_net_inflow, main_net_inflow_pct = _flow_value_from_spot_or_fund(spot, fund_flow)
    change_pct = _safe_float(spot.change_pct)
    volume_ratio = _safe_float(spot.volume_ratio)
    sector_strength = _safe_float(sector.get("strength_score"))
    sector_continuity = _calc_sector_continuity_score(sector)
    event_types = ["mainline_relay"]
    if _safe_float(main_net_inflow_pct) >= 3.0 or _safe_float(main_net_inflow) >= 30_000_000:
        event_types.append("capital")
    if (
        bool(kline_context.get("trend_breakout"))
        or bool(kline_context.get("near_trend_high"))
        or bool(kline_context.get("near_breakout"))
        or change_pct >= 2.0
    ):
        event_types.append("breakthrough")
    if change_pct >= 3.0 and volume_ratio >= 1.1:
        event_types.append("rapid_rise")
    if bool(kline_context.get("trend_acceleration_ready")):
        event_types.append("trend_breakout")
    has_fresh_hot_trigger = (
        change_pct >= FRESH_HOT_FIRST_BOARD_MIN_CHANGE_PCT
        and (
            _safe_float(main_net_inflow_pct) >= FRESH_HOT_FIRST_BOARD_MIN_MAIN_INFLOW_PCT
            or volume_ratio >= FRESH_HOT_FIRST_BOARD_MIN_BREAKTHROUGH_VOLUME_RATIO
        )
    )
    broad_rotation_cluster = _is_broad_rotation_cluster_sector(sector, market_context)
    broad_rotation_spread = (
        bool((market_context or {}).get("broad_first_board_overflow"))
        and (sector_strength >= BROAD_ROTATION_MAINLINE_MIN_SECTOR_STRENGTH or broad_rotation_cluster)
        and (
            sector_continuity >= BROAD_ROTATION_MAINLINE_MIN_SECTOR_CONTINUITY
            or bool(sector.get("sector_low_position_rotation"))
            or _safe_float(sector.get("sector_limit_up_delta")) >= 2.0
            or broad_rotation_cluster
        )
        and (-6.5 if broad_rotation_cluster else MAINLINE_SPREAD_MIN_CHANGE_PCT) <= change_pct <= MAINLINE_SPREAD_MAX_CHANGE_PCT
        and volume_ratio >= (0.35 if broad_rotation_cluster else 0.55)
    )
    broad_rotation_member_setup = (
        bool((market_context or {}).get("broad_first_board_overflow"))
        and (sector_strength >= 55.0 or broad_rotation_cluster)
        and (
            _safe_int(sector.get("limit_up_count")) >= 4
            or _safe_float(sector.get("sector_rotation_score")) >= 80.0
            or _safe_float(sector.get("sector_limit_up_delta")) >= 2.0
            or broad_rotation_cluster
        )
        and (-6.5 if broad_rotation_cluster else -2.5) <= change_pct <= MAINLINE_SPREAD_MAX_CHANGE_PCT
        and volume_ratio >= (0.35 if broad_rotation_cluster else 0.4)
        and _safe_float(spot.turnover) <= HOT_MAINLINE_RELAY_MAX_TURNOVER
    )
    if (
        not has_fresh_hot_trigger
        and (
            (
                sector_strength >= MAINLINE_SPREAD_MIN_SECTOR_STRENGTH
                and sector_continuity >= MAINLINE_SPREAD_MIN_SECTOR_CONTINUITY
            )
            or broad_rotation_spread
            or broad_rotation_member_setup
        )
        and (
            -6.5
            if broad_rotation_cluster
            else -2.5
            if broad_rotation_member_setup
            else MAINLINE_SPREAD_MIN_CHANGE_PCT
        ) <= change_pct <= MAINLINE_SPREAD_MAX_CHANGE_PCT
        and volume_ratio >= (
            0.35
            if broad_rotation_cluster
            else 0.4
            if broad_rotation_member_setup
            else 0.55
            if broad_rotation_spread
            else HOT_MAINLINE_RELAY_MIN_VOLUME_RATIO
        )
    ):
        event_types.insert(1, "mainline_spread")
    event_types = list(dict.fromkeys(event_types))

    bull_score = _safe_float(bull_row.get("total_score"))
    setup_grade = "A2 盘口确认后执行" if bull_score >= 72 and _safe_float(main_net_inflow_pct) >= 3.0 else "B类观察候选"
    detail = {
        "price": _safe_float(spot.price),
        "prev_close": _safe_float(spot.prev_close),
        "open": _safe_float(spot.open),
        "high": _safe_float(spot.high),
        "low": _safe_float(spot.low),
        "change_pct": change_pct,
        "main_net_inflow": main_net_inflow,
        "main_net_inflow_pct": main_net_inflow_pct,
        "amount": _safe_float(spot.amount),
        "volume_ratio": volume_ratio,
        "support_strength_score": _safe_float(spot.support_strength_score),
        "turnover": _safe_float(spot.turnover),
        "amplitude": _safe_float(spot.amplitude),
        "broad_rotation_spread": broad_rotation_spread,
        "broad_rotation_member_setup": broad_rotation_member_setup,
        "broad_rotation_cluster_setup": broad_rotation_cluster,
        "as_of": spot.updated_at.isoformat() if getattr(spot, "updated_at", None) else "",
    }
    sector_name = str(sector.get("sector_name") or "")
    secondary_parts = [
        f"{sector_name}强度{round(_safe_float(sector.get('strength_score')))}" if sector_name else "",
        f"板块涨停{_safe_int(sector.get('limit_up_count'))}只" if _safe_int(sector.get("limit_up_count")) else "",
        str(kline_context.get("summary") or "").strip(),
        " / ".join(str(item or "").strip() for item in (bull_row.get("top_signals") or [])[:3] if str(item or "").strip()),
    ]
    return {
        "code": str(spot.code or "").strip(),
        "name": str(spot.name or "").strip(),
        "event_types": event_types,
        "setup_grade": setup_grade,
        "setup_grade_display": setup_grade,
        "display_score": round(max(bull_score, 58.0) + _safe_float(sector.get("strength_score")) * 0.16, 2),
        "detail": detail,
        "risk_flags": [],
        "a1_blockers": [],
        "primary_reason": "热门主线扩散补涨预热",
        "secondary_reason": " · ".join(part for part in secondary_parts if part),
        "driver_primary": sector_name,
        "latest_as_of": detail["as_of"],
    }


def _build_broad_rotation_first_board_bull_row(
    spot: StockSpot,
    sector: dict,
    kline_context: dict,
    fund_flow: FundFlow | None = None,
    existing_row: dict | None = None,
) -> dict:
    if existing_row:
        return existing_row

    main_net_inflow, main_net_inflow_pct = _flow_value_from_spot_or_fund(spot, fund_flow)
    sector_strength = _safe_float(sector.get("strength_score"))
    rotation_score = _safe_float(sector.get("sector_rotation_score"))
    support_strength = _safe_float(spot.support_strength_score)
    kline_score = _safe_float(kline_context.get("confirmation_score")) * 100.0
    change_pct = _safe_float(spot.change_pct)
    volume_ratio = _safe_float(spot.volume_ratio)
    flow_score = _fund_range_score(main_net_inflow_pct, ideal_low=0.0, ideal_high=8.0, min_value=-7.0, max_value=16.0)
    change_fit = _range_score(change_pct, ideal_low=-1.5, ideal_high=4.8, min_value=-6.8, max_value=8.8)
    volume_fit = _range_score(volume_ratio, ideal_low=0.55, ideal_high=2.8, min_value=0.25, max_value=5.8)
    total_score = min(
        90.0,
        round(
            sector_strength * 0.30
            + rotation_score * 0.18
            + support_strength * 0.18
            + kline_score * 0.12
            + flow_score * 0.10
            + change_fit * 0.07
            + volume_fit * 0.05
            + min(max(_safe_int(sector.get("limit_up_count")), 0), 8) * 1.1
            + min(max(_safe_float(sector.get("sector_limit_up_delta")), 0.0), 4.0) * 1.2
            + (4.0 if _safe_float(main_net_inflow) >= 30_000_000 else 0.0),
            2,
        ),
    )
    top_signals = ["板块扩散反推"]
    if bool(sector.get("sector_low_position_rotation")):
        top_signals.append("低位轮动")
    if _safe_int(sector.get("limit_up_count")) >= 3:
        top_signals.append(f"板块涨停{_safe_int(sector.get('limit_up_count'))}只")
    if change_pct < 0:
        top_signals.append("低位回调待扩散")
    elif change_pct <= 2.0:
        top_signals.append("平开蓄势")
    return {
        "code": str(spot.code or "").strip(),
        "name": str(spot.name or "").strip(),
        "total_score": total_score,
        "level": "A" if total_score >= 76 else "B" if total_score >= 60 else "C",
        "top_signals": top_signals,
    }


def _build_broad_rotation_first_board_row(
    spot: StockSpot,
    *,
    sector: dict,
    bull_row: dict,
    kline_context: dict,
    fund_flow: FundFlow | None = None,
    market_context: dict | None = None,
) -> dict:
    main_net_inflow, main_net_inflow_pct = _flow_value_from_spot_or_fund(spot, fund_flow)
    change_pct = _safe_float(spot.change_pct)
    volume_ratio = _safe_float(spot.volume_ratio)
    sector_name = str(sector.get("sector_name") or "")
    sector_strength = _safe_float(sector.get("strength_score"))
    rotation_score = _safe_float(sector.get("sector_rotation_score"))
    cluster_setup = _is_broad_rotation_cluster_sector(sector, market_context)
    washout_reversal_watch = bool(sector.get("sector_washout_reversal_watch"))
    washout_prediction_seed = bool(
        washout_reversal_watch
        and bool(getattr(spot, "_promotion_washout_reserved", False))
        and (
            _safe_int(sector.get("sector_stock_count")) <= 0
            or _safe_int(sector.get("sector_stock_count")) <= SECTOR_WASHOUT_WATCH_MAX_MEMBER_COUNT
        )
        and _safe_float(sector.get("sector_washout_score")) >= 70.0
        and SECTOR_WASHOUT_WATCH_MIN_CHANGE_PCT <= change_pct <= 1.0
        and 0.25 <= volume_ratio <= 8.0
    )
    event_types = ["mainline_spread"]
    if bool(sector.get("sector_low_position_rotation")) or cluster_setup:
        event_types.append("mainline_relay")
    if _safe_float(main_net_inflow_pct) >= 1.5 or _safe_float(main_net_inflow) >= 20_000_000:
        event_types.append("capital")
    if change_pct >= 0.3 or bool(kline_context.get("near_breakout")) or bool(kline_context.get("trend_breakout")):
        event_types.append("breakthrough")
    if change_pct >= 3.0 and volume_ratio >= 0.8:
        event_types.append("rapid_rise")

    bull_score = _safe_float(bull_row.get("total_score"))
    setup_grade = (
        "A2 盘口确认后执行"
        if bull_score >= 64 and sector_strength >= 50 and not washout_reversal_watch
        else "B类观察候选"
    )
    detail = {
        "price": _safe_float(spot.price),
        "prev_close": _safe_float(spot.prev_close),
        "open": _safe_float(spot.open),
        "high": _safe_float(spot.high),
        "low": _safe_float(spot.low),
        "change_pct": change_pct,
        "main_net_inflow": main_net_inflow,
        "main_net_inflow_pct": main_net_inflow_pct,
        "amount": _safe_float(spot.amount),
        "volume_ratio": volume_ratio,
        "support_strength_score": _safe_float(spot.support_strength_score),
        "turnover": _safe_float(spot.turnover),
        "amplitude": _safe_float(spot.amplitude),
        "broad_rotation_spread": True,
        "broad_rotation_member_setup": True,
        "broad_rotation_cluster_setup": cluster_setup,
        "sector_catalyst_spread": True,
        "sector_washout_reversal_watch": washout_reversal_watch,
        "sector_washout_score": round(_safe_float(sector.get("sector_washout_score")), 2),
        "sector_recent_active_days": _safe_int(sector.get("sector_recent_active_days")),
        "sector_recent_limit_up_sum": _safe_int(sector.get("sector_recent_limit_up_sum")),
        "sector_retained_leader_ratio": round(_safe_float(sector.get("sector_retained_leader_ratio")), 3),
        # 形态种子进入次日预测样本，但在竞价/盘中确认前不成为交易买点。
        "prediction_shape_seed": washout_prediction_seed,
        "as_of": spot.updated_at.isoformat() if getattr(spot, "updated_at", None) else "",
    }
    secondary_parts = [
        f"{sector_name}强度{round(sector_strength)}" if sector_name else "",
        f"轮动分{rotation_score:.0f}" if rotation_score else "",
        f"板块涨停{_safe_int(sector.get('limit_up_count'))}只" if _safe_int(sector.get("limit_up_count")) else "",
        "活跃主线急跌洗盘，等待竞价集体修复" if washout_reversal_watch else str(sector.get("sector_rotation_label") or ""),
        str(kline_context.get("summary") or ""),
        " / ".join(str(item or "").strip() for item in (bull_row.get("top_signals") or [])[:3] if str(item or "").strip()),
    ]
    return {
        "key": f"{str(spot.code or '').strip()}-broad-rotation-first-board",
        "code": str(spot.code or "").strip(),
        "name": str(spot.name or "").strip(),
        "event_type": "mainline_spread",
        "event_types": list(dict.fromkeys(event_types)),
        "event_badges": [{"key": "mainline_spread", "label": "板块扩散"}],
        "event_count": len(event_types),
        "level": "A" if bull_score >= 76 else "B",
        "score": bull_score,
        "display_score": _clamp_score(bull_score + sector_strength * 0.16 + rotation_score * 0.08),
        "detail": detail,
        "description": " · ".join(part for part in secondary_parts if part),
        "setup_grade": setup_grade,
        "setup_grade_display": setup_grade,
        "setup_track": "",
        "risk_flags": [],
        "a1_blockers": [],
        "feishu_pushable": False,
        "buy_point_reached": False,
        "buy_point_pushable": False,
        "is_ipo_recent": False,
        "is_one_word_board": False,
        "latest_as_of": detail["as_of"],
        "primary_reason": "活跃主线急跌洗盘反转预备" if washout_reversal_watch else "板块级催化扩散补涨预备",
        "secondary_reason": " · ".join(part for part in secondary_parts if part),
        "driver_primary": sector_name or "板块扩散",
        "driver_secondary": f"板块强度{sector_strength:.0f} · 轮动{rotation_score:.0f}",
        "driver_peer_summary": "",
        "driver_detail_lines": [],
        "reference_driver_lines": [],
        "events": [],
    }


def _build_news_catalyst_bull_row(
    spot: StockSpot,
    sector: dict,
    news_context: dict,
    fund_flow: FundFlow | None = None,
    existing_row: dict | None = None,
) -> dict:
    _main_net_inflow, main_net_inflow_pct = _flow_value_from_spot_or_fund(spot, fund_flow)
    catalyst_score = _safe_float(news_context.get("news_catalyst_score"))
    sector_strength = _safe_float(sector.get("strength_score"))
    support_strength = _safe_float(spot.support_strength_score)
    change_pct = _safe_float(spot.change_pct)
    volume_ratio = _safe_float(spot.volume_ratio)
    total_score = min(
        92.0,
        round(
            catalyst_score * 0.32
            + sector_strength * 0.20
            + support_strength * 0.20
            + _range_score(change_pct, ideal_low=0.8, ideal_high=6.8, min_value=-2.0, max_value=9.2) * 0.14
            + _range_score(volume_ratio, ideal_low=0.9, ideal_high=2.8, min_value=0.3, max_value=5.0) * 0.10
            + _fund_range_score(main_net_inflow_pct, ideal_low=2.0, ideal_high=10.0, min_value=-5.0, max_value=16.0) * 0.12,
            2,
        ),
    )
    catalyst_row = {
        "code": str(spot.code or "").strip(),
        "name": str(spot.name or "").strip(),
        "total_score": total_score,
        "level": "A" if total_score >= 78 else "B" if total_score >= 64 else "C",
        "top_signals": ["消息催化", str(news_context.get("news_title") or "")[:18]],
    }
    if not existing_row or total_score >= _safe_float(existing_row.get("total_score")):
        return catalyst_row
    return {
        **existing_row,
        "top_signals": list(
            dict.fromkeys(
                [
                    *(existing_row.get("top_signals") or []),
                    *(catalyst_row.get("top_signals") or []),
                ]
            )
        ),
    }


def _build_news_catalyst_row(
    spot: StockSpot,
    *,
    sector: dict,
    bull_row: dict,
    news_context: dict,
    fund_flow: FundFlow | None = None,
) -> dict:
    main_net_inflow, main_net_inflow_pct = _flow_value_from_spot_or_fund(spot, fund_flow)
    catalyst_score = _safe_float(news_context.get("news_catalyst_score"))
    change_pct = _safe_float(spot.change_pct)
    volume_ratio = _safe_float(spot.volume_ratio)
    event_types = ["news_catalyst"]
    if _safe_float(main_net_inflow_pct) >= 2.0 or _safe_float(main_net_inflow) >= 20_000_000:
        event_types.append("capital")
    if change_pct >= 0.8 and volume_ratio >= 0.8:
        event_types.append("breakthrough")
    if change_pct >= 3.0 or _safe_int(news_context.get("news_importance")) >= 8:
        event_types.append("rapid_rise")
    bull_score = _safe_float(bull_row.get("total_score"))
    setup_grade = "A2 盘口确认后执行" if catalyst_score >= 58 and bull_score >= 55 else "B类观察候选"
    detail = {
        "price": _safe_float(spot.price),
        "prev_close": _safe_float(spot.prev_close),
        "open": _safe_float(spot.open),
        "high": _safe_float(spot.high),
        "low": _safe_float(spot.low),
        "change_pct": change_pct,
        "main_net_inflow": main_net_inflow,
        "main_net_inflow_pct": main_net_inflow_pct,
        "amount": _safe_float(spot.amount),
        "volume_ratio": volume_ratio,
        "support_strength_score": _safe_float(spot.support_strength_score),
        "turnover": _safe_float(spot.turnover),
        "amplitude": _safe_float(spot.amplitude),
        **news_context,
        "as_of": spot.updated_at.isoformat() if getattr(spot, "updated_at", None) else "",
    }
    title = str(news_context.get("news_title") or "")
    sector_name = str(sector.get("sector_name") or "")
    related_sector = str((news_context.get("news_related_sectors") or [""])[0] or "")
    return {
        "key": f"{str(spot.code or '').strip()}-news-catalyst",
        "code": str(spot.code or "").strip(),
        "name": str(spot.name or "").strip(),
        "event_type": "news_catalyst",
        "event_types": list(dict.fromkeys(event_types)),
        "event_badges": [{"key": "news_catalyst", "label": "消息催化"}],
        "event_count": len(event_types),
        "level": "A" if catalyst_score >= 70 else "B",
        "score": catalyst_score,
        "display_score": _clamp_score(catalyst_score + min(max(change_pct, 0.0), 8.0) * 2.0),
        "detail": detail,
        "description": str(news_context.get("news_impact_reason") or title),
        "setup_grade": setup_grade,
        "setup_grade_display": setup_grade,
        "setup_track": "",
        "risk_flags": [],
        "a1_blockers": [],
        "feishu_pushable": False,
        "buy_point_reached": False,
        "buy_point_pushable": False,
        "is_ipo_recent": False,
        "is_one_word_board": False,
        "latest_as_of": detail["as_of"],
        "primary_reason": f"消息催化：{title[:48]}",
        "secondary_reason": str(news_context.get("news_impact_reason") or ""),
        "driver_primary": sector_name or related_sector or "消息催化",
        "driver_secondary": f"消息强度{catalyst_score:.0f} · 来源{news_context.get('news_source') or '--'}",
        "driver_peer_summary": "",
        "driver_detail_lines": [],
        "reference_driver_lines": [],
        "events": [],
    }


def _auction_effective_change_pct(spot: StockSpot, auction_context: dict) -> float:
    auction_change = _safe_float(auction_context.get("auction_open_change"))
    auction_trade_date = str(auction_context.get("auction_trade_date") or "")
    spot_updated_at = getattr(spot, "updated_at", None)
    if auction_trade_date:
        try:
            if not isinstance(spot_updated_at, datetime) or spot_updated_at.date() < date.fromisoformat(auction_trade_date):
                return auction_change
        except ValueError:
            pass
    return _safe_float(spot.change_pct, auction_change)


def _build_auction_surge_bull_row(
    spot: StockSpot,
    sector: dict,
    auction_context: dict,
    fund_flow: FundFlow | None = None,
    existing_row: dict | None = None,
) -> dict:
    if existing_row:
        return existing_row
    _main_net_inflow, main_net_inflow_pct = _flow_value_from_spot_or_fund(spot, fund_flow)
    auction_score = _safe_float(auction_context.get("auction_strength_score"))
    sector_strength = _safe_float(sector.get("strength_score"))
    support_strength = _safe_float(spot.support_strength_score)
    change_pct = _auction_effective_change_pct(spot, auction_context)
    total_score = min(
        92.0,
        round(
            auction_score * 0.34
            + support_strength * 0.22
            + sector_strength * 0.18
            + _range_score(change_pct, ideal_low=2.0, ideal_high=7.2, min_value=-1.0, max_value=9.2) * 0.14
            + _fund_range_score(main_net_inflow_pct, ideal_low=2.0, ideal_high=12.0, min_value=-5.0, max_value=18.0) * 0.12,
            2,
        ),
    )
    return {
        "code": str(spot.code or "").strip(),
        "name": str(spot.name or "").strip(),
        "total_score": total_score,
        "level": "A" if total_score >= 78 else "B" if total_score >= 64 else "C",
        "top_signals": ["竞价强攻", f"高开{_safe_float(auction_context.get('auction_open_change')):.1f}%"],
    }


def _build_auction_surge_row(
    spot: StockSpot,
    *,
    sector: dict,
    bull_row: dict,
    auction_context: dict,
    fund_flow: FundFlow | None = None,
) -> dict:
    main_net_inflow, main_net_inflow_pct = _flow_value_from_spot_or_fund(spot, fund_flow)
    auction_score = _safe_float(auction_context.get("auction_strength_score"))
    change_pct = _auction_effective_change_pct(spot, auction_context)
    auction_price = _safe_float(auction_context.get("auction_price"))
    auction_volume_ratio = _safe_float(auction_context.get("auction_volume_ratio"))
    effective_volume_ratio = auction_volume_ratio if auction_volume_ratio > 0 else _safe_float(spot.volume_ratio)
    auction_trade_date = str(auction_context.get("auction_trade_date") or "")
    auction_time = str(auction_context.get("auction_time") or "")
    auction_as_of = f"{auction_trade_date}T{auction_time}" if auction_trade_date and auction_time else ""
    event_types = ["auction_surge"]
    if _safe_float(main_net_inflow_pct) >= 2.0 or _safe_float(main_net_inflow) >= 20_000_000:
        event_types.append("capital")
    if change_pct >= 1.0 or _safe_float(auction_context.get("auction_open_change")) >= 3.0:
        event_types.append("breakthrough")
    if change_pct >= 3.0:
        event_types.append("rapid_rise")
    bull_score = _safe_float(bull_row.get("total_score"))
    setup_grade = "A2 盘口确认后执行" if auction_score >= 58 and bull_score >= 52 else "B类观察候选"
    detail = {
        "price": auction_price or _safe_float(spot.price),
        "prev_close": _safe_float(spot.prev_close),
        "open": auction_price or _safe_float(spot.open),
        "high": max(auction_price, _safe_float(spot.high)) if auction_price > 0 else _safe_float(spot.high),
        "low": _safe_float(spot.low),
        "change_pct": change_pct,
        "main_net_inflow": main_net_inflow,
        "main_net_inflow_pct": main_net_inflow_pct,
        "amount": _safe_float(spot.amount),
        "volume_ratio": effective_volume_ratio,
        "support_strength_score": _safe_float(spot.support_strength_score),
        "turnover": _safe_float(spot.turnover),
        "amplitude": _safe_float(spot.amplitude),
        **auction_context,
        "as_of": auction_as_of or (spot.updated_at.isoformat() if getattr(spot, "updated_at", None) else ""),
    }
    sector_name = str(sector.get("sector_name") or "")
    return {
        "key": f"{str(spot.code or '').strip()}-auction-surge",
        "code": str(spot.code or "").strip(),
        "name": str(spot.name or "").strip(),
        "event_type": "auction_surge",
        "event_types": list(dict.fromkeys(event_types)),
        "event_badges": [{"key": "auction_surge", "label": "竞价强攻"}],
        "event_count": len(event_types),
        "level": "A" if auction_score >= 70 else "B",
        "score": auction_score,
        "display_score": _clamp_score(auction_score + min(max(change_pct, 0.0), 8.0) * 1.5),
        "detail": detail,
        "description": (
            f"{auction_context.get('auction_sector_name') or '同板块'}竞价集体转强"
            f"{_safe_int(auction_context.get('auction_sector_member_count'))}只，"
            f"个股高开{_safe_float(auction_context.get('auction_open_change')):.1f}%"
            if bool(auction_context.get("auction_cluster_confirmed"))
            else f"竞价高开{_safe_float(auction_context.get('auction_open_change')):.1f}%，竞价强度{auction_score:.0f}"
        ),
        "setup_grade": setup_grade,
        "setup_grade_display": setup_grade,
        "setup_track": "",
        "risk_flags": [],
        "a1_blockers": [],
        "feishu_pushable": False,
        "buy_point_reached": False,
        "buy_point_pushable": False,
        "is_ipo_recent": False,
        "is_one_word_board": _safe_float(auction_context.get("auction_open_change")) >= 9.0,
        "latest_as_of": detail["as_of"],
        "primary_reason": (
            f"板块竞价集体修复：{auction_context.get('auction_sector_name') or '同板块'}"
            f"{_safe_int(auction_context.get('auction_sector_member_count'))}只转强"
            if bool(auction_context.get("auction_cluster_confirmed"))
            else f"竞价高开强攻：高开{_safe_float(auction_context.get('auction_open_change')):.1f}%"
        ),
        "secondary_reason": f"竞价额{_safe_float(auction_context.get('auction_amount')) / 1e8:.2f}亿，量比{_safe_float(auction_context.get('auction_volume_ratio')):.2f}",
        "driver_primary": sector_name or "竞价强攻",
        "driver_secondary": f"竞价强度{auction_score:.0f}",
        "driver_peer_summary": "",
        "driver_detail_lines": [],
        "reference_driver_lines": [],
        "events": [],
    }


def _build_pre_board_probe_bull_row(
    context: dict,
    sector: dict,
    existing_row: dict | None = None,
) -> dict:
    if existing_row:
        return existing_row
    route = str(context.get("candidate_route") or "")
    route_score = (
        _safe_float(context.get("oversold_reversal_score"))
        if route == "oversold_reversal_start"
        else _safe_float(context.get("pre_board_probe_score"))
    )
    sector_strength = _safe_float(sector.get("strength_score"))
    support_strength = _safe_float(context.get("support_strength_score"))
    volume_fit = _range_score(
        _safe_float(
            context.get("board_memory_volume_ratio_20")
            if bool(context.get("board_memory_reset_ready"))
            else context.get("low_base_volume_ratio_5_20")
            if bool(context.get("low_base_rotation_ready"))
            else context.get("volume_ratio")
        ),
        ideal_low=0.45 if bool(context.get("board_memory_reset_ready")) else 0.62 if bool(context.get("low_base_rotation_ready")) else 1.1,
        ideal_high=1.15 if bool(context.get("board_memory_reset_ready")) else 1.08 if bool(context.get("low_base_rotation_ready")) else 3.8,
        min_value=0.30 if bool(context.get("board_memory_reset_ready")) else 0.45 if bool(context.get("low_base_rotation_ready")) else 0.5,
        max_value=1.85 if bool(context.get("board_memory_reset_ready")) else 1.60 if bool(context.get("low_base_rotation_ready")) else 7.0,
    )
    turnover_fit = _range_score(_safe_float(context.get("turnover")), ideal_low=1.2, ideal_high=10.5, min_value=0.4, max_value=26.0)
    total_score = min(
        90.0,
        round(
            route_score * 0.34
            + support_strength * 0.24
            + sector_strength * 0.16
            + volume_fit * 0.14
            + turnover_fit * 0.12,
            2,
        ),
    )
    top_signals = ["跌后反包预备"] if route == "oversold_reversal_start" else ["涨停试盘"]
    if bool(context.get("limit_probe")):
        top_signals.append("盘中冲高试板")
    if bool(context.get("touch_board_pullback")):
        top_signals.append("摸板回落蓄势")
    if bool(context.get("burst_pullback_restart_ready")):
        top_signals.append("爆量深调再启动")
    if bool(context.get("trend_preheat")):
        top_signals.append("短均主升预热")
    if bool(context.get("momentum_shakeout_ready")):
        top_signals.append("中期动量缩量洗盘")
    if bool(context.get("board_memory_reset_ready")):
        top_signals.append("历史涨停记忆低位回撤")
    if bool(context.get("low_base_rotation_ready")):
        top_signals.append("无涨停记忆低位首波")
    if bool(context.get("washout_reversal")) or bool(context.get("panic_flush")):
        top_signals.append("恐慌释放")
    if _safe_float(sector.get("strength_score")) >= 45:
        top_signals.append("板块有强度")
    return {
        "code": str(context.get("code") or "").strip(),
        "name": str(context.get("name") or "").strip(),
        "total_score": total_score,
        "level": "A" if total_score >= 76 else "B" if total_score >= 62 else "C",
        "top_signals": top_signals,
    }


def _build_contextual_first_board_bull_row(
    *,
    row: dict,
    spot: StockSpot | None,
    sector: dict,
    kline_context: dict,
    news_context: dict | None = None,
    auction_context: dict | None = None,
    pre_board_context: dict | None = None,
    hot_mainline_spot: StockSpot | None = None,
    fund_flow: FundFlow | None = None,
    existing_row: dict | None = None,
) -> dict:
    if existing_row:
        return existing_row

    candidates: list[dict] = []
    usable_spot = spot or hot_mainline_spot
    if usable_spot is not None and news_context:
        candidates.append(
            _build_news_catalyst_bull_row(
                usable_spot,
                sector,
                news_context,
                fund_flow=fund_flow,
            )
        )
    if usable_spot is not None and auction_context:
        candidates.append(
            _build_auction_surge_bull_row(
                usable_spot,
                sector,
                auction_context,
                fund_flow=fund_flow,
            )
        )
    if pre_board_context:
        candidates.append(
            _build_pre_board_probe_bull_row(
                pre_board_context,
                sector,
            )
        )
    if hot_mainline_spot is not None and kline_context:
        candidates.append(
            _build_hot_mainline_relay_bull_row(
                hot_mainline_spot,
                sector,
                kline_context,
                fund_flow=fund_flow,
            )
        )
    if usable_spot is not None and kline_context:
        event_types = {str(item or "") for item in row.get("event_types") or []}
        if event_types.intersection({"stealth_setup", "breakthrough", "rapid_rise", "capital"}):
            candidates.append(
                _build_quiet_setup_bull_row(
                    usable_spot,
                    sector,
                    kline_context,
                )
            )

    candidates = [item for item in candidates if _safe_float(item.get("total_score")) > 0]
    if not candidates:
        return {}
    return max(
        candidates,
        key=lambda item: (
            _safe_float(item.get("total_score")),
            SETUP_GRADE_RANK.get(str(item.get("level") or ""), -1),
        ),
    )


def _build_pre_board_probe_row(
    context: dict,
    *,
    sector: dict,
    bull_row: dict,
    fund_flow: FundFlow | None = None,
) -> dict:
    route = str(context.get("candidate_route") or "pre_board_probe_start")
    event_type = str(context.get("event_type") or "pre_board_probe")
    route_score = (
        _safe_float(context.get("oversold_reversal_score"))
        if route == "oversold_reversal_start"
        else _safe_float(context.get("pre_board_probe_score"))
    )
    main_net_inflow = _safe_float(getattr(fund_flow, "main_net_inflow", 0.0)) if fund_flow is not None else 0.0
    main_net_inflow_pct = _safe_float(getattr(fund_flow, "main_net_inflow_pct", 0.0)) if fund_flow is not None else 0.0
    event_types = [event_type]
    if _safe_float(main_net_inflow_pct) >= 2.0 or _safe_float(main_net_inflow) >= 20_000_000:
        event_types.append("capital")
    if bool(context.get("limit_probe")) or bool(context.get("preheat_breakout")):
        event_types.append("breakthrough")
    if bool(context.get("touch_board_pullback")) or bool(context.get("burst_pullback_restart_ready")):
        event_types.append("breakthrough")
    if bool(context.get("panic_flush")) or bool(context.get("washout_reversal")):
        event_types.append("rapid_rise")
    bull_score = _safe_float(bull_row.get("total_score"))
    setup_grade = "A2 盘口确认后执行" if route_score >= 62 and bull_score >= 55 else "B类观察候选"
    detail = {
        "price": _safe_float(context.get("price")),
        "prev_close": _safe_float(context.get("prev_close")),
        "open": _safe_float(context.get("open")),
        "high": _safe_float(context.get("high")),
        "low": _safe_float(context.get("low")),
        "change_pct": _safe_float(context.get("change_pct")),
        "main_net_inflow": main_net_inflow,
        "main_net_inflow_pct": main_net_inflow_pct,
        "amount": _safe_float(context.get("amount")),
        "volume_ratio": _safe_float(context.get("volume_ratio")),
        "support_strength_score": _safe_float(context.get("support_strength_score")),
        "turnover": _safe_float(context.get("turnover")),
        "amplitude": _safe_float(context.get("amplitude")),
        "pre_board_probe_score": _safe_float(context.get("pre_board_probe_score")),
        "oversold_reversal_score": _safe_float(context.get("oversold_reversal_score")),
        "intraday_high_pct": _safe_float(context.get("intraday_high_pct")),
        "upper_gap_pct": _safe_float(context.get("upper_gap_pct")),
        "lower_gap_pct": _safe_float(context.get("lower_gap_pct")),
        "close_position": _safe_float(context.get("close_position")),
        "limit_probe": bool(context.get("limit_probe")),
        "preheat_breakout": bool(context.get("preheat_breakout")),
        "touch_board_pullback": bool(context.get("touch_board_pullback")),
        "trend_preheat": bool(context.get("trend_preheat")),
        "washout_reversal": bool(context.get("washout_reversal")),
        "panic_flush": bool(context.get("panic_flush")),
        "pre_board_precursor_type": str(context.get("pre_board_precursor_type") or ""),
        "pre_board_precursor_label": str(context.get("pre_board_precursor_label") or ""),
        "momentum_shakeout_ready": bool(context.get("momentum_shakeout_ready")),
        "momentum_shakeout_score": _safe_float(context.get("momentum_shakeout_score")),
        "momentum_shakeout_stats": context.get("momentum_shakeout_stats") or {},
        "strong_first_bearish_ready": bool(context.get("strong_first_bearish_ready")),
        "strong_first_bearish_score": _safe_float(context.get("strong_first_bearish_score")),
        "strong_first_bearish_stats": context.get("strong_first_bearish_stats") or {},
        "board_memory_reset_ready": bool(context.get("board_memory_reset_ready")),
        "board_memory_reset_score": _safe_float(context.get("board_memory_reset_score")),
        "board_memory_days_since_limit_up": _safe_int(context.get("board_memory_days_since_limit_up")),
        "board_memory_last_limit_up_date": str(context.get("board_memory_last_limit_up_date") or ""),
        "board_memory_position_120": _safe_float(context.get("board_memory_position_120"), 1.0),
        "board_memory_return_5d": _safe_float(context.get("board_memory_return_5d")),
        "board_memory_return_20d": _safe_float(context.get("board_memory_return_20d")),
        "board_memory_volume_ratio_20": _safe_float(context.get("board_memory_volume_ratio_20")),
        "board_memory_distance_ma20": _safe_float(context.get("board_memory_distance_ma20")),
        "board_memory_high_20_gap": _safe_float(context.get("board_memory_high_20_gap")),
        "low_base_rotation_ready": bool(context.get("low_base_rotation_ready")),
        "low_base_rotation_score": _safe_float(context.get("low_base_rotation_score")),
        "low_base_position_120": _safe_float(context.get("low_base_position_120"), 1.0),
        "low_base_return_5d": _safe_float(context.get("low_base_return_5d")),
        "low_base_return_20d": _safe_float(context.get("low_base_return_20d")),
        "low_base_return_60d": _safe_float(context.get("low_base_return_60d")),
        "low_base_volume_ratio_5_20": _safe_float(context.get("low_base_volume_ratio_5_20")),
        "low_base_distance_ma20": _safe_float(context.get("low_base_distance_ma20")),
        "low_base_high_20_gap": _safe_float(context.get("low_base_high_20_gap")),
        "low_base_prior_impulse": _safe_float(context.get("low_base_prior_impulse")),
        "prediction_shape_seed": bool(context.get("prediction_shape_seed")),
        "burst_pullback_restart_ready": bool(context.get("burst_pullback_restart_ready")),
        "burst_pullback_score": _safe_float(context.get("burst_pullback_score")),
        "burst_pullback_label": str(context.get("burst_pullback_label") or ""),
        "burst_date": str(context.get("burst_date") or ""),
        "burst_volume_ratio": _safe_float(context.get("burst_volume_ratio")),
        "burst_pullback_depth_pct": _safe_float(context.get("burst_pullback_depth_pct")),
        "burst_shrink_ratio": _safe_float(context.get("burst_shrink_ratio"), 1.0),
        "burst_rebound_pct": _safe_float(context.get("burst_rebound_pct")),
        "burst_high_gap_pct": _safe_float(context.get("burst_high_gap_pct")),
        "burst_restart_days": _safe_int(context.get("burst_restart_days")),
        "burst_restart_volume_ratio": _safe_float(context.get("burst_restart_volume_ratio")),
        "as_of": str(context.get("as_of") or ""),
    }
    sector_name = str(sector.get("sector_name") or "")
    if route == "oversold_reversal_start":
        if bool(context.get("strong_first_bearish_ready")):
            primary_reason = "强趋势首阴回收预备"
            secondary_core = (
                f"前日跌幅{_safe_float(context.get('change_pct')):.1f}%"
                f" · 技术持续分{_safe_float((context.get('strong_first_bearish_stats') or {}).get('pullback_technical_persistence_score')):.0f}"
                " · 次日只等收回确认"
            )
        else:
            primary_reason = "跌后反包首板预备"
            secondary_core = f"前日跌幅{_safe_float(context.get('change_pct')):.1f}% · 上影释放{_safe_float(context.get('upper_gap_pct')):.1f}%"
    elif bool(context.get("board_memory_reset_ready")):
        primary_reason = "历史涨停记忆低位回撤预备"
        secondary_core = (
            f"距上次涨停{_safe_int(context.get('board_memory_days_since_limit_up'))}日"
            f" · 120日位置{_safe_float(context.get('board_memory_position_120')):.0%}"
            f" · 20日量比{_safe_float(context.get('board_memory_volume_ratio_20')):.2f}"
        )
    elif bool(context.get("low_base_rotation_ready")):
        primary_reason = "低位首波轮动预备"
        secondary_core = (
            f"120日位置{_safe_float(context.get('low_base_position_120')):.0%}"
            f" · 20日涨幅{_safe_float(context.get('low_base_return_20d')):+.1f}%"
            f" · 5/20日量比{_safe_float(context.get('low_base_volume_ratio_5_20')):.2f}"
        )
    else:
        primary_reason = "涨停试盘首板预备"
        secondary_core = f"盘中最高{_safe_float(context.get('intraday_high_pct')):.1f}% · 量比{_safe_float(context.get('volume_ratio')):.2f}"
    return {
        "key": f"{str(context.get('code') or '').strip()}-{route}",
        "code": str(context.get("code") or "").strip(),
        "name": str(context.get("name") or "").strip(),
        "event_type": event_type,
        "event_types": list(dict.fromkeys(event_types)),
        "event_badges": [{"key": event_type, "label": FIRST_BOARD_ROUTE_LABELS.get(route, route)}],
        "event_count": len(event_types),
        "level": "A" if route_score >= 70 else "B",
        "score": route_score,
        "display_score": _clamp_score(route_score + bull_score * 0.18 + _safe_float(sector.get("strength_score")) * 0.12),
        "detail": detail,
        "description": secondary_core,
        "setup_grade": setup_grade,
        "setup_grade_display": setup_grade,
        "setup_track": "",
        "risk_flags": [],
        "a1_blockers": [],
        "feishu_pushable": False,
        "buy_point_reached": False,
        "buy_point_pushable": False,
        "is_ipo_recent": False,
        "is_one_word_board": False,
        "latest_as_of": detail["as_of"],
        "primary_reason": primary_reason,
        "secondary_reason": " · ".join(item for item in [secondary_core, sector_name] if item),
        "driver_primary": sector_name or FIRST_BOARD_ROUTE_LABELS.get(route, primary_reason),
        "driver_secondary": f"路线强度{route_score:.0f}",
        "driver_peer_summary": "",
        "driver_detail_lines": [],
        "reference_driver_lines": [],
        "events": [],
    }


def _enrich_first_board_row_with_context(
    row: dict,
    *,
    news_context: dict | None = None,
    auction_context: dict | None = None,
    pre_board_context: dict | None = None,
    hot_mainline_row: dict | None = None,
) -> dict:
    if not news_context and not auction_context and not pre_board_context and not hot_mainline_row:
        return row

    detail = {**(row.get("detail") or {})}
    event_prefixes: list[str] = []
    secondary_prefixes: list[str] = []
    enriched = {**row}

    if news_context:
        detail.update(news_context)
        event_prefixes.append("news_catalyst")
        title = str(news_context.get("news_title") or "").strip()
        source = str(news_context.get("news_source") or "").strip()
        secondary_prefixes.append(
            f"消息催化：{title[:36]}{f' · {source}' if source else ''}"
            if title
            else "消息催化"
        )
        if not str(enriched.get("driver_primary") or "").strip():
            related_sector = str((news_context.get("news_related_sectors") or [""])[0] or "").strip()
            enriched["driver_primary"] = related_sector or "消息催化"

    if auction_context:
        detail.update(auction_context)
        event_prefixes.append("auction_surge")
        secondary_prefixes.append(
            f"竞价高开{_safe_float(auction_context.get('auction_open_change')):.1f}%"
            f" · 强度{_safe_float(auction_context.get('auction_strength_score')):.0f}"
        )
        if not str(enriched.get("driver_primary") or "").strip():
            enriched["driver_primary"] = "竞价强攻"

    if pre_board_context:
        detail.update(
            {
                key: value
                for key, value in pre_board_context.items()
                if key
                not in {
                    "code",
                    "name",
                    "candidate_route",
                    "event_type",
                }
            }
        )
        route = str(pre_board_context.get("candidate_route") or "")
        event_type = str(pre_board_context.get("event_type") or "")
        if event_type:
            event_prefixes.append(event_type)
        secondary_prefixes.append(
            "跌后反包预备"
            if route == "oversold_reversal_start"
            else "历史涨停记忆低位回撤"
            if bool(pre_board_context.get("board_memory_reset_ready"))
            else "低位首波轮动预备"
            if bool(pre_board_context.get("low_base_rotation_ready"))
            else "涨停试盘预备"
        )
        if not str(enriched.get("driver_primary") or "").strip():
            enriched["driver_primary"] = FIRST_BOARD_ROUTE_LABELS.get(route, "首板预热")

    if hot_mainline_row:
        detail.update(hot_mainline_row.get("detail") or {})
        event_prefixes.extend(
            str(item or "").strip()
            for item in (hot_mainline_row.get("event_types") or [])
            if str(item or "").strip()
        )
        hot_secondary = str(hot_mainline_row.get("secondary_reason") or "").strip()
        if hot_secondary:
            secondary_prefixes.append(hot_secondary)
        if not str(enriched.get("driver_primary") or "").strip():
            enriched["driver_primary"] = str(hot_mainline_row.get("driver_primary") or "主线扩散")

    enriched["detail"] = detail
    enriched["event_types"] = list(
        dict.fromkeys(
            event_prefixes
            + [str(item or "").strip() for item in (row.get("event_types") or []) if str(item or "").strip()]
        )
    )
    existing_secondary = str(row.get("secondary_reason") or "").strip()
    enriched["secondary_reason"] = " · ".join(
        item for item in [*secondary_prefixes, existing_secondary] if item
    )
    return enriched


def _resolve_fresh_hot_start_route(
    *,
    sector: dict,
    bull_score: float,
    support_strength: float,
) -> str:
    sector_strength = _safe_float(sector.get("strength_score"))
    sector_continuity = _calc_sector_continuity_score(sector)
    limit_up_count = _safe_int(sector.get("limit_up_count"))
    consecutive_days = _safe_int(sector.get("consecutive_days"))
    is_mainline_start = (
        sector_strength >= FRESH_MAINLINE_START_MIN_SECTOR_STRENGTH
        and sector_continuity >= FRESH_MAINLINE_START_MIN_SECTOR_CONTINUITY
        and (
            bull_score >= FRESH_MAINLINE_START_MIN_BULL_SCORE
            or support_strength >= FRESH_MAINLINE_START_MIN_SUPPORT_STRENGTH
            or (limit_up_count >= 3 and consecutive_days >= 2)
        )
    )
    return "fresh_mainline_start" if is_mainline_start else "fresh_relay_start"


def _candidate_route_from_strict_route(
    route: str | None,
    *,
    sector: dict | None = None,
    bull_score: float = 0.0,
    support_strength: float = 0.0,
) -> str | None:
    normalized = str(route or "").strip()
    if normalized == "strict_news_catalyst":
        return "news_catalyst_start"
    if normalized == "strict_auction_surge":
        return "auction_surge_start"
    if normalized == "strict_mainline_spread":
        return "mainline_spread_start"
    if normalized == "strict_pre_board_probe":
        return "pre_board_probe_start"
    if normalized == "strict_oversold_reversal":
        return "oversold_reversal_start"
    if normalized == "strict_hot_fresh":
        return _resolve_fresh_hot_start_route(
            sector=sector or {},
            bull_score=bull_score,
            support_strength=support_strength,
        )
    return None


def _build_first_board_probability(
    row: dict,
    bull_row: dict,
    sector: dict,
    market_context: dict,
    kline_context: dict | None = None,
    memory_features: dict | None = None,
    candidate_route: str | None = None,
) -> tuple[float, dict]:
    detail = row.get("detail") or {}
    setup_grade = str(row.get("setup_grade") or "")
    display_score = _safe_float(row.get("display_score"))
    bull_score = _safe_float(bull_row.get("total_score"))
    event_types = [str(item or "") for item in row.get("event_types") or []]
    main_inflow_pct = _safe_float(detail.get("main_net_inflow_pct"))
    volume_ratio = _safe_float(detail.get("volume_ratio"))
    support_strength = _safe_float(detail.get("support_strength_score"))
    change_pct = _safe_float(detail.get("change_pct"))
    sector_strength = _safe_float(sector.get("strength_score"))
    sector_continuity = _calc_sector_continuity_score(sector)
    sector_rotation_score = _safe_float(sector.get("sector_rotation_score"))
    sector_strength_delta = _safe_float(sector.get("sector_strength_delta"))
    sector_limit_up_delta = _safe_float(sector.get("sector_limit_up_delta"))
    has_low_position_rotation = bool(sector.get("sector_low_position_rotation"))
    has_crowded_stale_theme = bool(sector.get("sector_crowded_stale_theme"))
    sector_catalyst_spread = bool(detail.get("sector_catalyst_spread"))
    broad_rotation_cluster_setup = bool(detail.get("broad_rotation_cluster_setup")) or _is_broad_rotation_cluster_sector(sector, market_context)
    has_high_board_risk = any(
        str((flag or {}).get("code") or "") == "high_board_break_risk"
        for flag in (row.get("risk_flags") or [])
    )
    risk_penalty = min(len(row.get("risk_flags") or []), 3) * 0.04
    blocker_penalty = min(len(row.get("a1_blockers") or []), 2) * 0.03
    kline_context = kline_context or {}
    memory_features = memory_features or {}

    event_bonus = sum(FIRST_BOARD_EVENT_BONUS.get(item, 0) for item in set(event_types))
    kline_bonus = _safe_float(kline_context.get("confirmation_bonus"))
    kline_penalty = _safe_float(kline_context.get("confirmation_penalty"))
    memory_score = _safe_float(memory_features.get("memory_score"))
    long_cycle_profile = dict(kline_context.get("long_cycle_profile") or {})
    has_long_cycle_profile = bool(long_cycle_profile)
    long_cycle_regime = str(long_cycle_profile.get("long_cycle_regime") or "neutral")
    long_cycle_quality_score = _safe_float(long_cycle_profile.get("quality_setup_score"))
    long_cycle_shape_ready = bool(long_cycle_profile.get("shape_ready"))
    platform_relaunch_meta = _build_platform_relaunch_signal_meta(kline_context, memory_features)
    support_squeeze_meta = _build_support_squeeze_signal_meta(kline_context, detail)
    limit_probe_shape_meta = _build_first_board_limit_probe_shape(detail, kline_context)
    limit_probe_shape_bonus_points = _safe_float(limit_probe_shape_meta.get("limit_probe_shape_bonus_points"))
    limit_probe_shape_penalty_points = _safe_float(limit_probe_shape_meta.get("limit_probe_shape_penalty_points"))
    platform_relaunch_signal_score = _safe_float(platform_relaunch_meta.get("limit_up_nearby_signal_score"))
    support_squeeze_signal_score = _safe_float(support_squeeze_meta.get("support_squeeze_signal_score"))
    news_catalyst_score = _safe_float(detail.get("news_catalyst_score"))
    news_direct_high_impact = bool(detail.get("news_direct_high_impact"))
    news_repeated_direct = bool(detail.get("news_repeated_direct"))
    news_information_phase = str(detail.get("news_information_phase") or "")
    auction_strength_score = _safe_float(detail.get("auction_strength_score"))
    auction_feed_complete = auction_context_complete(detail)
    pre_board_probe_score = _safe_float(detail.get("pre_board_probe_score"))
    oversold_reversal_score = _safe_float(detail.get("oversold_reversal_score"))
    strong_first_bearish_ready = bool(detail.get("strong_first_bearish_ready"))
    strong_first_bearish_score = _safe_float(detail.get("strong_first_bearish_score"))
    strong_first_bearish_stats = dict(detail.get("strong_first_bearish_stats") or {})
    strong_first_bearish_persistence = _safe_float(
        strong_first_bearish_stats.get("pullback_technical_persistence_score")
    )
    funding_preheat_ready = bool(detail.get("funding_preheat_ready"))
    funding_preheat_score = _safe_float(detail.get("funding_preheat_score"))
    funding_persistent_outflow_risk = bool(detail.get("funding_persistent_outflow_risk"))
    low_base_sector_ignition_ready = bool(detail.get("low_base_sector_ignition_ready"))
    low_base_sector_ignition_confirmed = bool(detail.get("low_base_sector_ignition_confirmed"))
    low_base_sector_ignition_score = _safe_float(detail.get("low_base_sector_ignition_score"))
    launch_direction_logit_delta = _safe_float(detail.get("launch_direction_logit_delta"))
    launch_strong_rise_logit_delta = _safe_float(detail.get("launch_strong_rise_logit_delta"))
    market_limit_up_count = _safe_int(market_context.get("limit_up_count"))
    market_board_height = _safe_int(market_context.get("board_height"))
    sealed_ratio = _safe_float(market_context.get("sealed_ratio")) * 100.0
    market_score = _clamp_score(
        min(market_limit_up_count, 60) / 60.0 * 35.0
        + min(market_board_height, 5) / 5.0 * 35.0
        + min(max(sealed_ratio, 0.0), 100.0) * 0.30
        - (10.0 if bool(market_context.get("weak_seal_market")) else 0.0)
    )
    platform_score = _clamp_score(
        _safe_float(kline_context.get("confirmation_score")) * 45.0
        + _inverse_score(_safe_float(kline_context.get("platform_range_pct")), good=8.5, bad=18.0) * 0.16
        + _inverse_score(_safe_float(kline_context.get("volume_suffocation_ratio"), 1.0), good=0.62, bad=1.08) * 0.18
        + _inverse_score(_safe_float(kline_context.get("overhead_gap_pct")), good=2.6, bad=8.0) * 0.16
        + min(_safe_int(kline_context.get("strict_confirmation_count")), 4) / 4.0 * 100.0 * 0.05
    )
    theme_score = _clamp_score(
        sector_continuity * 45.0
        + min(sector_strength, 100.0) * 0.30
        + min(_safe_int(sector.get("limit_up_count")), 5) / 5.0 * 15.0
        + min(_safe_int(sector.get("consecutive_days")), 4) / 4.0 * 10.0
        + (
            min(sector_rotation_score, 100.0) * 0.13
            if has_low_position_rotation
            else min(max(sector_strength_delta, 0.0), 24.0) * 0.28
        )
        - (7.0 if has_crowded_stale_theme else 0.0)
    )
    close_strength = _calc_close_strength(detail)
    trigger_score = _clamp_score(
        min(support_strength, 100.0) * 0.30
        + _range_score(volume_ratio, ideal_low=1.2, ideal_high=2.8, min_value=0.4, max_value=5.0) * 0.22
        + _range_score(main_inflow_pct, ideal_low=5.0, ideal_high=12.0, min_value=-5.0, max_value=18.0) * 0.20
        + (18.0 if "breakthrough" in event_types else 12.0 if "capital" in event_types else 0.0)
        + close_strength * 10.0
    )
    base_signal_score = _clamp_score(
        min(max(display_score, 0), 160) / 160.0 * 24.0
        + min(max(bull_score, 0), 100) * 0.24
        + {"A1 可直接执行": 18.0, "A2 盘口确认后执行": 12.0, "B类观察候选": 7.0}.get(setup_grade, 4.0)
        + ((min(event_bonus, 0.14) / 0.14 * 10.0) if event_bonus > 0 else 0.0)
    )
    distribution_penalty = _high_board_distribution_penalty(
        market_context,
        sector_continuity=sector_continuity,
        has_high_board_risk=has_high_board_risk,
        turnover=_safe_float(detail.get("turnover")),
        break_count=0,
    )
    memory_days_since_last = _safe_int(memory_features.get("memory_days_since_last_limit_up"), 999)
    route = candidate_route or ""
    if not route:
        if bool(platform_relaunch_meta.get("platform_relaunch_event_ready")):
            route = "platform_relaunch"
        elif "stealth_setup" in event_types and bool(support_squeeze_meta.get("support_squeeze_ready")):
            route = "support_squeeze_start"
        elif (
            {"capital", "breakthrough"}.intersection(event_types)
            and memory_score < FRESH_HOT_FIRST_BOARD_MAX_MEMORY_SCORE
            and support_strength >= FRESH_HOT_FIRST_BOARD_MIN_SUPPORT_STRENGTH
            and sector_strength >= FRESH_HOT_FIRST_BOARD_MIN_SECTOR_STRENGTH
            and sector_continuity >= FRESH_HOT_FIRST_BOARD_MIN_SECTOR_CONTINUITY
            and (
                main_inflow_pct >= FRESH_HOT_FIRST_BOARD_MIN_MAIN_INFLOW_PCT
                or volume_ratio >= FRESH_HOT_FIRST_BOARD_MIN_BREAKTHROUGH_VOLUME_RATIO
            )
            and change_pct >= FRESH_HOT_FIRST_BOARD_MIN_CHANGE_PCT
        ):
            route = _resolve_fresh_hot_start_route(
                sector=sector,
                bull_score=bull_score,
                support_strength=support_strength,
            )
        elif "auction_surge" in event_types:
            route = "auction_surge_start"
        elif "news_catalyst" in event_types:
            route = "news_catalyst_start"
        elif "mainline_spread" in event_types:
            route = "mainline_spread_start"
        elif "pre_board_probe" in event_types:
            route = "pre_board_probe_start"
        elif "oversold_reversal" in event_types:
            route = "oversold_reversal_start"
        elif "stealth_setup" in event_types:
            route = "quiet_setup"
        elif "mainline_relay" in event_types:
            route = (
                _resolve_fresh_hot_start_route(
                    sector=sector,
                    bull_score=bull_score,
                    support_strength=support_strength,
                )
                if memory_score < FRESH_HOT_FIRST_BOARD_MAX_MEMORY_SCORE
                else "relay_fillup"
            )
        elif {"capital", "breakthrough"}.intersection(event_types):
            route = "hot_primary"
        else:
            route = "relay_fillup"

    risk_points = (
        min(len(row.get("risk_flags") or []), 3) * 4.0
        + min(len(row.get("a1_blockers") or []), 2) * 3.0
        + distribution_penalty * 35.0
        + _safe_int(kline_context.get("failed_reversal_count")) * 4.0
        + (6.0 if has_high_board_risk else 0.0)
        + kline_penalty * 18.0
    )
    risk_points += _market_regime_penalty(market_context, target_board=1, route=route)
    rotation_route_points = 0.0
    if has_low_position_rotation:
        rotation_route_points = min(max(sector_rotation_score - 48.0, 0.0), 34.0) * 0.18
        if route in {
            "news_catalyst_start",
            "auction_surge_start",
            "mainline_spread_start",
            "pre_board_probe_start",
            "oversold_reversal_start",
            "fresh_relay_start",
        }:
            rotation_route_points += 1.8
    elif broad_rotation_cluster_setup:
        rotation_route_points = (
            1.6
            + min(max(_safe_int(sector.get("limit_up_count")) - BROAD_ROTATION_MAINLINE_MIN_LIMIT_UP_COUNT, 0), 18) * 0.12
            + min(max(sector_limit_up_delta, 0.0), 5.0) * 0.22
        )
    elif sector_strength_delta >= 8.0 or sector_limit_up_delta >= 2.0:
        rotation_route_points = min(max(sector_strength_delta, sector_limit_up_delta * 4.0, 0.0), 24.0) * 0.10
    broad_rotation_route_points = 0.0
    broad_rotation_mainline_cluster_points = 0.0
    if (
        (bool(market_context.get("broad_first_board_overflow")) or sector_catalyst_spread)
        and route == "mainline_spread_start"
        and (sector_strength >= BROAD_ROTATION_MAINLINE_MIN_SECTOR_STRENGTH or broad_rotation_cluster_setup)
        and (
            has_low_position_rotation
            or sector_continuity >= BROAD_ROTATION_MAINLINE_MIN_SECTOR_CONTINUITY
            or sector_limit_up_delta >= 2.0
            or broad_rotation_cluster_setup
        )
    ):
        broad_rotation_route_points = 1.4 + min(max(sector_rotation_score - 48.0, 0.0), 30.0) * 0.08
        if sector_limit_up_delta >= 2.0:
            broad_rotation_route_points += 0.8
        if broad_rotation_cluster_setup:
            broad_rotation_mainline_cluster_points = (
                3.2
                + min(max(_safe_int(sector.get("limit_up_count")) - BROAD_ROTATION_MAINLINE_MIN_LIMIT_UP_COUNT, 0), 16) * 0.22
                + min(max(support_strength - 48.0, 0.0), 22.0) * 0.08
                + min(max(_safe_int(kline_context.get("strict_confirmation_count")) - 1, 0), 3) * 0.70
            )
            broad_rotation_route_points += broad_rotation_mainline_cluster_points
        if support_strength >= 50.0:
            broad_rotation_route_points += 0.6
    broad_rotation_reversal_points = 0.0
    if (
        bool(market_context.get("broad_first_board_overflow"))
        and route == "oversold_reversal_start"
        and (
            broad_rotation_cluster_setup
            or _safe_int(sector.get("limit_up_count")) >= BROAD_ROTATION_MAINLINE_MIN_LIMIT_UP_COUNT
        )
        and oversold_reversal_score >= BROAD_ROTATION_REVERSAL_MIN_OVERSOLD_SCORE
        and support_strength >= BROAD_ROTATION_REVERSAL_MIN_SUPPORT_SCORE
        and -7.5 <= change_pct <= -1.5
        and 0.50 <= volume_ratio <= 1.80
    ):
        broad_rotation_reversal_points = (
            4.0
            + min(max(oversold_reversal_score - BROAD_ROTATION_REVERSAL_MIN_OVERSOLD_SCORE, 0.0), 16.0) * 0.22
            + min(max(support_strength - BROAD_ROTATION_REVERSAL_MIN_SUPPORT_SCORE, 0.0), 20.0) * 0.10
            + min(max(_safe_int(sector.get("limit_up_count")) - 5, 0), 18) * 0.10
        )
        if broad_rotation_cluster_setup:
            broad_rotation_reversal_points += 0.8
        if _safe_int(kline_context.get("strict_confirmation_count")) >= 2:
            broad_rotation_reversal_points += 0.7
    stale_theme_penalty_points = 0.0
    if has_crowded_stale_theme:
        stale_theme_penalty_points = 4.0 if route in {
            "mainline_spread_start",
            "pre_board_probe_start",
            "fresh_relay_start",
            "relay_fillup",
            "hot_primary",
        } else 2.0
    if route == "platform_relaunch":
        route_score = (
            memory_score * 0.27
            + platform_score * 0.29
            + theme_score * 0.18
            + trigger_score * 0.18
            + market_score * 0.08
            + platform_relaunch_signal_score * 0.12
            - risk_points
        )
    elif route == "support_squeeze_start":
        route_score = (
            platform_score * 0.28
            + trigger_score * 0.22
            + theme_score * 0.18
            + base_signal_score * 0.12
            + support_squeeze_signal_score * 0.14
            + market_score * 0.06
            - risk_points
        )
    elif route == "news_catalyst_start":
        route_score = (
            news_catalyst_score * 0.30
            + trigger_score * 0.22
            + theme_score * 0.18
            + base_signal_score * 0.12
            + platform_score * 0.10
            + market_score * 0.08
            - risk_points
        )
    elif route == "auction_surge_start":
        route_score = (
            auction_strength_score * 0.30
            + trigger_score * 0.25
            + theme_score * 0.16
            + base_signal_score * 0.12
            + platform_score * 0.09
            + market_score * 0.08
            - risk_points
        )
    elif route == "mainline_spread_start":
        route_score = (
            theme_score * 0.30
            + trigger_score * 0.23
            + platform_score * 0.16
            + base_signal_score * 0.12
            + market_score * 0.11
            + min(_safe_int(sector.get("limit_up_count")), 6) / 6.0 * 100.0 * 0.08
            - risk_points
        )
    elif route == "pre_board_probe_start":
        route_score = (
            pre_board_probe_score * 0.30
            + trigger_score * 0.22
            + platform_score * 0.16
            + theme_score * 0.14
            + base_signal_score * 0.10
            + market_score * 0.08
            - risk_points
        )
    elif route == "oversold_reversal_start":
        route_score = (
            oversold_reversal_score * 0.32
            + trigger_score * 0.18
            + theme_score * 0.16
            + platform_score * 0.12
            + base_signal_score * 0.12
            + market_score * 0.10
            - risk_points
        )
    elif route in {"fresh_hot_start", "fresh_mainline_start", "fresh_relay_start"}:
        theme_weight = 0.28 if route == "fresh_mainline_start" else 0.20
        trigger_weight = 0.26 if route == "fresh_mainline_start" else 0.32
        signal_weight = 0.20 if route == "fresh_mainline_start" else 0.24
        platform_weight = 0.14 if route == "fresh_mainline_start" else 0.16
        market_weight = 0.12 if route == "fresh_mainline_start" else 0.08
        route_score = (
            base_signal_score * signal_weight
            + trigger_score * trigger_weight
            + theme_score * theme_weight
            + platform_score * platform_weight
            + market_score * market_weight
            - risk_points
        )
    elif route == "quiet_setup":
        route_score = (
            platform_score * 0.33
            + theme_score * 0.22
            + trigger_score * 0.18
            + memory_score * 0.14
            + base_signal_score * 0.08
            + market_score * 0.05
            - risk_points
        )
    elif route == "hot_primary":
        route_score = (
            base_signal_score * 0.32
            + trigger_score * 0.24
            + theme_score * 0.18
            + platform_score * 0.12
            + memory_score * 0.08
            + market_score * 0.06
            - risk_points
        )
    else:
        route_score = (
            base_signal_score * 0.30
            + platform_score * 0.25
            + theme_score * 0.18
            + trigger_score * 0.15
            + memory_score * 0.07
            + market_score * 0.05
            - risk_points
        )

    strong_first_bearish_confirmation = bool(
        strong_first_bearish_ready
        and (
            (
                str(detail.get("news_mapping_mode") or "") == "direct_code"
                and str(detail.get("news_event_grade") or "") in {"hard", "medium"}
            )
            or bool(detail.get("auction_cluster_confirmed"))
            or sector_rotation_score >= 58.0
            or sector_limit_up_delta >= 2.0
        )
    )
    strong_first_bearish_points = 0.0
    if strong_first_bearish_ready:
        # 首阴形态只负责进入专用召回通道。只有直接事件、竞价集群或板块
        # 轮动确认后才给显著加分，避免把高位长阴批量包装成“高胜率”。
        strong_first_bearish_points = min(
            max(strong_first_bearish_persistence - 66.0, 0.0) * 0.16,
            3.2,
        )
        strong_first_bearish_points += (
            3.0 if strong_first_bearish_confirmation else -0.8
        )

    # 历史 lift 仅支持小幅排序修正：行业点火+活跃修复约1.295，三日资金
    # 预热约1.217。不能把这些稀疏特征直接换算成高概率，更不能绕过买点。
    launch_precursor_points = 0.0
    if low_base_sector_ignition_ready:
        launch_precursor_points += 1.2 + min(
            max(low_base_sector_ignition_score - 60.0, 0.0) * 0.04,
            1.2,
        )
    if low_base_sector_ignition_confirmed:
        launch_precursor_points += 0.8
    elif funding_preheat_ready:
        launch_precursor_points += 0.5
    if funding_persistent_outflow_risk:
        launch_precursor_points -= 1.2

    news_precursor_points = 0.0
    if route == "news_catalyst_start" and str(detail.get("news_mapping_mode") or "") == "direct_code":
        news_precursor_points += 1.2 if news_direct_high_impact else 0.0
        news_precursor_points += 0.8 if news_repeated_direct else 0.0
        # 隔夜消息只在显式 as-of 已覆盖收盘后窗口时存在；历史收盘回放不会
        # 读取该字段，因此这里不会把次日早盘信息泄漏进上一收盘预测。
        news_precursor_points += 0.6 if news_information_phase == "overnight" else 0.0

    route_score = _clamp_score(
        route_score
        + rotation_route_points
        + broad_rotation_route_points
        + broad_rotation_reversal_points
        - stale_theme_penalty_points
        + limit_probe_shape_bonus_points
        - limit_probe_shape_penalty_points
        + strong_first_bearish_points
        + launch_precursor_points
        + news_precursor_points
    )
    next_day_score = route_score
    if bool(limit_probe_shape_meta.get("limit_probe_shape_supportive")) and route in {
        "pre_board_probe_start",
        "fresh_mainline_start",
        "fresh_relay_start",
        "hot_primary",
    }:
        next_day_score += 1.2
    if bool(limit_probe_shape_meta.get("limit_probe_shape_failed")):
        next_day_score -= 2.2
    if route in {"quiet_setup", "platform_relaunch"}:
        next_day_score -= 5.0
    if route in {"fresh_relay_start", "relay_fillup"}:
        next_day_score -= 4.0
    if bool(market_context.get("hostile_follow_through_market")) and not (
        change_pct >= 3.0 and main_inflow_pct >= 5.0 and support_strength >= 60.0
    ):
        next_day_score -= 4.0
    if broad_rotation_reversal_points > 0:
        next_day_score += min(broad_rotation_reversal_points * 0.18, 2.0)
    elif route == "mainline_spread_start" and broad_rotation_cluster_setup:
        next_day_score += min(broad_rotation_route_points * 0.18, 2.5)
    if has_low_position_rotation and route in {
        "news_catalyst_start",
        "auction_surge_start",
        "mainline_spread_start",
        "pre_board_probe_start",
        "oversold_reversal_start",
        "fresh_relay_start",
    }:
        next_day_score += 1.6
    if has_crowded_stale_theme and route in {"pre_board_probe_start", "mainline_spread_start", "relay_fillup", "hot_primary"}:
        next_day_score -= 2.4

    # 长周期形态只修正3-10日准备度，不抬高“次日首板”主概率，避免把
    # 250日低位/历史记忆误当成明天必涨的追板信号。
    long_cycle_five_day_score_delta = 0.0
    if long_cycle_regime == "high_overheat":
        long_cycle_five_day_score_delta = -8.0
    elif long_cycle_shape_ready:
        long_cycle_five_day_score_delta = min(
            6.0,
            max(1.5, (long_cycle_quality_score - 52.0) * 0.20),
        )
    elif (
        has_long_cycle_profile
        and long_cycle_regime == "neutral"
        and long_cycle_quality_score < 42.0
    ):
        long_cycle_five_day_score_delta = -2.0

    sub_probabilities = {
        "first_limitup_next_day": _score_to_probability(next_day_score, pivot=69.0, spread=7.5),
        "breakout_3d": _score_to_probability(
            route_score + long_cycle_five_day_score_delta * 0.45,
            pivot=58.0,
            spread=8.0,
        ),
        "first_limitup_5d": _score_to_probability(
            route_score + long_cycle_five_day_score_delta,
            pivot=66.0,
            spread=7.0,
        ),
        "become_core_10d": _score_to_probability(
            route_score + theme_score * 0.18 + long_cycle_five_day_score_delta * 0.35,
            pivot=72.0,
            spread=9.0,
        ),
    }
    sector_catalyst_probability_cap = 0.0
    if sector_catalyst_spread and route == "mainline_spread_start":
        next_day_cap = 0.60 if bool(market_context.get("broad_first_board_overflow")) else 0.52
        if change_pct < 0:
            next_day_cap -= 0.08
        if main_inflow_pct < 0 and not {"capital", "breakthrough"}.intersection(event_types):
            next_day_cap -= 0.08
        if volume_ratio < 0.55:
            next_day_cap -= 0.04
        next_day_cap = max(next_day_cap, 0.30)
        sector_catalyst_probability_cap = next_day_cap
        sub_probabilities["first_limitup_next_day"] = min(
            _safe_float(sub_probabilities.get("first_limitup_next_day")),
            next_day_cap,
        )
        sub_probabilities["first_limitup_5d"] = min(
            _safe_float(sub_probabilities.get("first_limitup_5d")),
            max(next_day_cap + 0.16, 0.46),
        )
    probability = sub_probabilities["first_limitup_next_day"]
    return probability, {
        "candidate_route": route,
        "candidate_route_label": (
            "低位行业点火修复"
            if low_base_sector_ignition_ready
            else "历史涨停记忆回撤"
            if route == "pre_board_probe_start" and bool(detail.get("board_memory_reset_ready"))
            else "低位首波轮动预备"
            if route == "pre_board_probe_start" and bool(detail.get("low_base_rotation_ready"))
            else FIRST_BOARD_ROUTE_LABELS.get(route, route or "首板候选")
        ),
        "route_score": route_score,
        "long_cycle_regime": long_cycle_regime,
        "long_cycle_regime_label": str(long_cycle_profile.get("long_cycle_regime_label") or ""),
        "long_cycle_quality_score": round(long_cycle_quality_score, 2),
        "long_cycle_shape_ready": long_cycle_shape_ready,
        "long_cycle_setup_phase": str(long_cycle_profile.get("setup_phase") or ""),
        "long_cycle_setup_phase_label": str(long_cycle_profile.get("setup_phase_label") or ""),
        "long_cycle_five_day_score_delta": round(long_cycle_five_day_score_delta, 2),
        "memory_score": memory_score,
        "platform_score": platform_score,
        "theme_score": theme_score,
        "trigger_score": trigger_score,
        "sector_code": str(sector.get("sector_code") or ""),
        "sector_name": str(sector.get("sector_name") or ""),
        "sector_type": str(sector.get("sector_type") or ""),
        "sector_strength_score": round(_safe_float(sector.get("strength_score")), 2),
        "sector_fund_flow": round(_safe_float(sector.get("fund_flow")), 2),
        "sector_mapping_confidence_score": round(
            _safe_float(sector.get("mapping_confidence_score")), 2
        ),
        "sector_reason_relevance": _safe_int(sector.get("reason_relevance")),
        "sector_selection_score": round(_safe_float(sector.get("sector_selection_score")), 2),
        "launch_feature_version": str(detail.get("launch_feature_version") or LAUNCH_PRECURSOR_FEATURE_VERSION),
        "launch_profile_ready": bool(kline_context.get("launch_profile_ready")),
        "launch_mid_low_repair": bool(kline_context.get("launch_mid_low_repair")),
        "launch_active_volume_turnover": bool(kline_context.get("launch_active_volume_turnover")),
        "launch_deep_low_risk": bool(kline_context.get("launch_deep_low_risk")),
        "launch_profile_score": round(_safe_float(kline_context.get("launch_profile_score")), 2),
        "launch_position_120": round(_safe_float(kline_context.get("launch_position_120"), 1.0), 4),
        "launch_return_5d": round(_safe_float(kline_context.get("launch_return_5d")), 2),
        "launch_return_20d": round(_safe_float(kline_context.get("launch_return_20d")), 2),
        "launch_return_60d": round(_safe_float(kline_context.get("launch_return_60d")), 2),
        "launch_distance_ma20": round(_safe_float(kline_context.get("launch_distance_ma20")), 2),
        "launch_volume_ratio_20": round(_safe_float(kline_context.get("launch_volume_ratio_20")), 3),
        "launch_turnover": round(_safe_float(kline_context.get("launch_turnover")), 3),
        "funding_coverage_days": _safe_int(detail.get("funding_coverage_days")),
        "funding_as_of_trade_date": str(detail.get("funding_as_of_trade_date") or ""),
        "funding_main_inflow_3d": round(_safe_float(detail.get("funding_main_inflow_3d")), 2),
        "funding_main_inflow_pct_3d": round(_safe_float(detail.get("funding_main_inflow_pct_3d")), 2),
        "funding_main_inflow_pct_5d": round(_safe_float(detail.get("funding_main_inflow_pct_5d")), 2),
        "funding_positive_days_3d": _safe_int(detail.get("funding_positive_days_3d")),
        "funding_acceleration": round(_safe_float(detail.get("funding_acceleration")), 2),
        "funding_preheat_ready": funding_preheat_ready,
        "funding_preheat_score": round(funding_preheat_score, 2),
        "funding_persistent_outflow_risk": funding_persistent_outflow_risk,
        "primary_industry_code": str(detail.get("primary_industry_code") or ""),
        "primary_industry_name": str(detail.get("primary_industry_name") or ""),
        "primary_industry_strength": round(_safe_float(detail.get("primary_industry_strength")), 2),
        "primary_industry_limit_up_count": _safe_int(detail.get("primary_industry_limit_up_count")),
        "primary_industry_rotation_score": round(_safe_float(detail.get("primary_industry_rotation_score")), 2),
        "primary_industry_strength_delta": round(_safe_float(detail.get("primary_industry_strength_delta")), 2),
        "primary_industry_lifecycle_state": str(detail.get("primary_industry_lifecycle_state") or ""),
        "primary_industry_low_position_rotation": bool(detail.get("primary_industry_low_position_rotation")),
        "primary_industry_ignition_ready": bool(detail.get("primary_industry_ignition_ready")),
        "primary_industry_ignition_score": round(_safe_float(detail.get("primary_industry_ignition_score")), 2),
        "low_base_sector_ignition_ready": low_base_sector_ignition_ready,
        "low_base_sector_ignition_confirmed": low_base_sector_ignition_confirmed,
        "low_base_sector_ignition_prediction_only": bool(detail.get("low_base_sector_ignition_prediction_only")),
        "low_base_sector_ignition_tier": str(detail.get("low_base_sector_ignition_tier") or "none"),
        "low_base_sector_ignition_score": round(low_base_sector_ignition_score, 2),
        "launch_evidence_log_lift": round(_safe_float(detail.get("launch_evidence_log_lift")), 4),
        "launch_direction_logit_delta": round(launch_direction_logit_delta, 4),
        "launch_strong_rise_logit_delta": round(launch_strong_rise_logit_delta, 4),
        "launch_probability_targets_split": bool(detail.get("launch_probability_targets_split")),
        "launch_precursor_rank_points": round(launch_precursor_points, 2),
        "news_precursor_rank_points": round(news_precursor_points, 2),
        "news_catalyst_score": round(news_catalyst_score, 2),
        "news_title": str(detail.get("news_title") or ""),
        "news_source": str(detail.get("news_source") or ""),
        "news_importance": _safe_int(detail.get("news_importance")),
        "news_count": _safe_int(detail.get("news_count")),
        "news_before_close_count": _safe_int(detail.get("news_before_close_count")),
        "news_after_close_count": _safe_int(detail.get("news_after_close_count")),
        "news_high_impact_count": _safe_int(detail.get("news_high_impact_count")),
        "news_hard_event_count": _safe_int(detail.get("news_hard_event_count")),
        "news_repeated_direct": bool(detail.get("news_repeated_direct")),
        "news_direct_high_impact": bool(detail.get("news_direct_high_impact")),
        "news_information_phase": str(detail.get("news_information_phase") or ""),
        "news_latest_publish_time": str(detail.get("news_latest_publish_time") or ""),
        "news_publish_time": str(detail.get("news_publish_time") or ""),
        "news_query_end_time": str(detail.get("news_query_end_time") or ""),
        "news_impact_scope": str(detail.get("news_impact_scope") or ""),
        "news_event_grade": str(detail.get("news_event_grade") or ""),
        "news_event_type": str(detail.get("news_event_type") or ""),
        "news_mapping_mode": str(detail.get("news_mapping_mode") or ""),
        "news_sector_inferred": bool(detail.get("news_sector_inferred")),
        "news_after_close": bool(detail.get("news_after_close")),
        "news_fresh_after_trade_close": bool(detail.get("news_fresh_after_trade_close")),
        "news_related_sectors": list(detail.get("news_related_sectors") or [])[:10],
        # 只冻结本次共享loader已提供的证据，不拿当前新闻回补旧预测。
        "news_evidence": deepcopy(detail.get("news_evidence")) if isinstance(detail.get("news_evidence"), dict) else {},
        "auction_strength_score": round(auction_strength_score, 2),
        "auction_open_change": round(_safe_float(detail.get("auction_open_change")), 2),
        "auction_baseline_open_change": round(
            _safe_float(detail.get("auction_baseline_open_change")),
            2,
        ),
        "auction_open_change_delta": round(
            _safe_float(detail.get("auction_open_change_delta")),
            2,
        ),
        "auction_reversal_confirmed": bool(detail.get("auction_reversal_confirmed")),
        "auction_volume_ratio": round(_safe_float(detail.get("auction_volume_ratio")), 3),
        "auction_amount": round(_safe_float(detail.get("auction_amount")), 2),
        "auction_cancelled": bool(detail.get("auction_cancelled")),
        "auction_feed_complete": auction_feed_complete,
        "auction_evidence_status": str(detail.get("auction_evidence_status") or "unknown"),
        "auction_evidence_contract": str(detail.get("auction_evidence_contract") or ""),
        "auction_cluster_confirmed": bool(detail.get("auction_cluster_confirmed")),
        "auction_sector_name": str(detail.get("auction_sector_name") or ""),
        "auction_sector_member_count": _safe_int(detail.get("auction_sector_member_count")),
        "auction_sector_avg_open_change": round(
            _safe_float(detail.get("auction_sector_avg_open_change")), 2
        ),
        "pre_board_probe_score": round(pre_board_probe_score, 2),
        "oversold_reversal_score": round(oversold_reversal_score, 2),
        "intraday_high_pct": round(_safe_float(detail.get("intraday_high_pct")), 2),
        "upper_gap_pct": round(_safe_float(detail.get("upper_gap_pct")), 2),
        "lower_gap_pct": round(_safe_float(detail.get("lower_gap_pct")), 2),
        "close_position": round(_safe_float(detail.get("close_position")), 3),
        "limit_probe": bool(detail.get("limit_probe")),
        "preheat_breakout": bool(detail.get("preheat_breakout")),
        "momentum_shakeout_ready": bool(detail.get("momentum_shakeout_ready")),
        "momentum_shakeout_score": round(_safe_float(detail.get("momentum_shakeout_score")), 2),
        "momentum_shakeout_stats": detail.get("momentum_shakeout_stats") or {},
        "strong_first_bearish_ready": strong_first_bearish_ready,
        "strong_first_bearish_score": round(strong_first_bearish_score, 2),
        "strong_first_bearish_stats": strong_first_bearish_stats,
        "strong_first_bearish_persistence_score": round(
            strong_first_bearish_persistence, 1
        ),
        "strong_first_bearish_confirmation": strong_first_bearish_confirmation,
        "strong_first_bearish_rank_points": round(
            strong_first_bearish_points, 2
        ),
        "board_memory_reset_ready": bool(detail.get("board_memory_reset_ready")),
        "board_memory_reset_score": round(_safe_float(detail.get("board_memory_reset_score")), 2),
        "board_memory_days_since_limit_up": _safe_int(detail.get("board_memory_days_since_limit_up")),
        "board_memory_last_limit_up_date": str(detail.get("board_memory_last_limit_up_date") or ""),
        "board_memory_position_120": round(_safe_float(detail.get("board_memory_position_120"), 1.0), 4),
        "board_memory_return_5d": round(_safe_float(detail.get("board_memory_return_5d")), 2),
        "board_memory_return_20d": round(_safe_float(detail.get("board_memory_return_20d")), 2),
        "board_memory_volume_ratio_20": round(_safe_float(detail.get("board_memory_volume_ratio_20")), 3),
        "board_memory_distance_ma20": round(_safe_float(detail.get("board_memory_distance_ma20")), 2),
        "board_memory_high_20_gap": round(_safe_float(detail.get("board_memory_high_20_gap")), 2),
        "low_base_rotation_ready": bool(detail.get("low_base_rotation_ready")),
        "low_base_rotation_score": round(_safe_float(detail.get("low_base_rotation_score")), 2),
        "low_base_position_120": round(_safe_float(detail.get("low_base_position_120"), 1.0), 4),
        "low_base_return_5d": round(_safe_float(detail.get("low_base_return_5d")), 2),
        "low_base_return_20d": round(_safe_float(detail.get("low_base_return_20d")), 2),
        "low_base_return_60d": round(_safe_float(detail.get("low_base_return_60d")), 2),
        "low_base_volume_ratio_5_20": round(_safe_float(detail.get("low_base_volume_ratio_5_20")), 3),
        "low_base_distance_ma20": round(_safe_float(detail.get("low_base_distance_ma20")), 2),
        "low_base_high_20_gap": round(_safe_float(detail.get("low_base_high_20_gap")), 2),
        "low_base_prior_impulse": round(_safe_float(detail.get("low_base_prior_impulse")), 2),
        "prediction_shape_seed": bool(detail.get("prediction_shape_seed")),
        "washout_reversal": bool(detail.get("washout_reversal")),
        "panic_flush": bool(detail.get("panic_flush")),
        "burst_pullback_restart_ready": bool(
            detail.get("burst_pullback_restart_ready") or kline_context.get("burst_pullback_restart_ready")
        ),
        "burst_pullback_score": round(
            max(_safe_float(detail.get("burst_pullback_score")), _safe_float(kline_context.get("burst_pullback_score"))),
            2,
        ),
        "burst_pullback_label": str(detail.get("burst_pullback_label") or kline_context.get("burst_pullback_label") or ""),
        "burst_date": str(detail.get("burst_date") or kline_context.get("burst_date") or ""),
        "burst_volume_ratio": round(
            max(_safe_float(detail.get("burst_volume_ratio")), _safe_float(kline_context.get("burst_volume_ratio"))),
            2,
        ),
        "burst_pullback_depth_pct": round(
            max(
                _safe_float(detail.get("burst_pullback_depth_pct")),
                _safe_float(kline_context.get("burst_pullback_depth_pct")),
            ),
            2,
        ),
        "burst_shrink_ratio": round(
            min(
                _safe_float(detail.get("burst_shrink_ratio"), 1.0),
                _safe_float(kline_context.get("burst_shrink_ratio"), 1.0),
            ),
            3,
        ),
        "burst_rebound_pct": round(
            max(_safe_float(detail.get("burst_rebound_pct")), _safe_float(kline_context.get("burst_rebound_pct"))),
            2,
        ),
        "burst_high_gap_pct": round(
            min(
                _safe_float(detail.get("burst_high_gap_pct"), 999.0),
                _safe_float(kline_context.get("burst_high_gap_pct"), 999.0),
            ),
            2,
        ),
        "burst_restart_days": max(
            _safe_int(detail.get("burst_restart_days")),
            _safe_int(kline_context.get("burst_restart_days")),
        ),
        "burst_restart_volume_ratio": round(
            max(
                _safe_float(detail.get("burst_restart_volume_ratio")),
                _safe_float(kline_context.get("burst_restart_volume_ratio")),
            ),
            3,
        ),
        **limit_probe_shape_meta,
        "market_score": market_score,
        "market_broad_first_board_overflow": bool(market_context.get("broad_first_board_overflow")),
        "market_washout_reversal_setup": bool(market_context.get("washout_reversal_setup")),
        "market_broad_rising": bool(market_context.get("broad_rising_market")),
        "market_up_ratio": round(_safe_float(market_context.get("market_up_ratio")), 4),
        "market_avg_change": round(_safe_float(market_context.get("market_avg_change")), 3),
        "market_first_board_count": _safe_int(market_context.get("first_board_count")),
        "sector_rotation_score": round(sector_rotation_score, 2),
        "sector_strength_delta_percentile": round(
            _safe_float(sector.get("sector_strength_delta_percentile")), 2
        ),
        "sector_limit_up_delta_percentile": round(
            _safe_float(sector.get("sector_limit_up_delta_percentile")), 2
        ),
        "sector_fund_flow_delta_percentile": round(
            _safe_float(sector.get("sector_fund_flow_delta_percentile")), 2
        ),
        "sector_breadth_percentile": round(_safe_float(sector.get("sector_breadth_percentile")), 2),
        "sector_flow_persistence_score": round(
            _safe_float(sector.get("sector_flow_persistence_score")), 2
        ),
        "sector_strength_delta": round(sector_strength_delta, 2),
        "sector_limit_up_delta": round(sector_limit_up_delta, 2),
        "sector_low_position_rotation": has_low_position_rotation,
        "sector_washout_reversal_watch": bool(sector.get("sector_washout_reversal_watch")),
        "sector_washout_score": round(_safe_float(sector.get("sector_washout_score")), 2),
        "sector_recent_active_days": _safe_int(sector.get("sector_recent_active_days")),
        "sector_recent_limit_up_sum": _safe_int(sector.get("sector_recent_limit_up_sum")),
        "sector_retained_leader_ratio": round(_safe_float(sector.get("sector_retained_leader_ratio")), 3),
        "sector_crowded_stale_theme": has_crowded_stale_theme,
        "sector_rotation_label": str(sector.get("sector_rotation_label") or ""),
        "rotation_route_points": round(rotation_route_points, 2),
        "broad_rotation_route_points": round(broad_rotation_route_points, 2),
        "broad_rotation_mainline_cluster_points": round(broad_rotation_mainline_cluster_points, 2),
        "broad_rotation_reversal_points": round(broad_rotation_reversal_points, 2),
        "sector_catalyst_probability_cap": round(sector_catalyst_probability_cap, 3),
        "stale_theme_penalty_points": round(stale_theme_penalty_points, 2),
        "broad_rotation_member_setup": bool(detail.get("broad_rotation_member_setup")),
        "broad_rotation_cluster_setup": broad_rotation_cluster_setup,
        "sector_catalyst_spread": sector_catalyst_spread,
        "market_risk_level": str(market_context.get("market_risk_level") or ""),
        "market_main_net_inflow": _safe_float(market_context.get("main_net_inflow")),
        "next_day_score": round(next_day_score, 2),
        "risk_penalty": round(risk_points, 3),
        "main_probability_name": FIRST_BOARD_MAIN_PROBABILITY_NAME,
        "sub_probabilities": sub_probabilities,
        "sector_continuity_score": round(sector_continuity, 3),
        "distribution_penalty": round(distribution_penalty, 3),
        "has_high_board_risk": has_high_board_risk,
        "kline_confirmation_score": round(_safe_float(kline_context.get("confirmation_score")), 3),
        "kline_confirmation_bonus": round(kline_bonus, 3),
        "kline_confirmation_penalty": round(kline_penalty, 3),
        "has_recent_limit_up_event": bool(platform_relaunch_meta.get("has_recent_limit_up_event")),
        "platform_relaunch_event_ready": bool(platform_relaunch_meta.get("platform_relaunch_event_ready")),
        "memory_last_limit_up_anchor_price": round(
            _safe_float(platform_relaunch_meta.get("memory_last_limit_up_anchor_price")),
            2,
        ),
        "limit_up_anchor_gap_pct": round(_safe_float(platform_relaunch_meta.get("limit_up_anchor_gap_pct")), 2),
        "limit_up_nearby_doji_confirmation": bool(platform_relaunch_meta.get("limit_up_nearby_doji_confirmation")),
        "limit_up_nearby_signal_score": round(platform_relaunch_signal_score, 2),
        "limit_up_nearby_pattern_label": str(platform_relaunch_meta.get("limit_up_nearby_pattern_label") or ""),
        "support_squeeze_ready": bool(support_squeeze_meta.get("support_squeeze_ready")),
        "support_squeeze_only_ready": bool(support_squeeze_meta.get("support_squeeze_only_ready")),
        "support_squeeze_signal_score": round(support_squeeze_signal_score, 2),
        "support_volume_release_ready": bool(support_squeeze_meta.get("support_volume_release_ready")),
        "support_volume_release_signal_score": round(_safe_float(support_squeeze_meta.get("support_volume_release_signal_score")), 2),
        "support_squeeze_pattern_label": str(support_squeeze_meta.get("support_squeeze_pattern_label") or ""),
        "support_zone_price": round(_safe_float(support_squeeze_meta.get("support_zone_price")), 2),
        "support_zone_gap_pct": round(_safe_float(support_squeeze_meta.get("support_zone_gap_pct")), 2),
        "support_hold_days": _safe_int(support_squeeze_meta.get("support_hold_days")),
        "support_rebound_count": _safe_int(support_squeeze_meta.get("support_rebound_count")),
        "lower_shadow_absorption": round(_safe_float(support_squeeze_meta.get("lower_shadow_absorption")), 3),
        "has_support_absorption": bool(support_squeeze_meta.get("has_support_absorption")),
        "has_doji_confirmation": bool(kline_context.get("has_doji_confirmation")),
        "qualified_doji_confirmation": bool(kline_context.get("qualified_doji_confirmation")),
        "has_platform_contraction": bool(kline_context.get("has_platform_contraction")),
        "has_volume_contraction": bool(kline_context.get("has_volume_contraction")),
        "has_volume_suffocation": bool(kline_context.get("has_volume_suffocation")),
        "near_trend_high": bool(kline_context.get("near_trend_high")),
        "trend_breakout": bool(kline_context.get("trend_breakout")),
        "trend_acceleration_ready": bool(kline_context.get("trend_acceleration_ready")),
        "ma_alignment": bool(kline_context.get("ma_alignment")),
        "trend_volume_release": bool(kline_context.get("trend_volume_release")),
        "trend_breakout_gap_pct": round(_safe_float(kline_context.get("trend_breakout_gap_pct")), 2),
        "platform_confirmation_count": _safe_int(kline_context.get("platform_confirmation_count")),
        "trend_confirmation_count": _safe_int(kline_context.get("trend_confirmation_count")),
        "platform_cycle_type": str(kline_context.get("platform_cycle_type") or ""),
        "platform_cycle_label": str(kline_context.get("platform_cycle_label") or ""),
        "platform_cycle_days": _safe_int(kline_context.get("platform_cycle_days")),
        "strict_confirmation_count": _safe_int(kline_context.get("strict_confirmation_count")),
        "strict_ready": bool(kline_context.get("strict_ready")),
        "volume_suffocation_ratio": round(_safe_float(kline_context.get("volume_suffocation_ratio")), 3),
        "overhead_gap_pct": round(_safe_float(kline_context.get("overhead_gap_pct")), 2),
        "failed_reversal_count": _safe_int(kline_context.get("failed_reversal_count")),
        "close_strength_pct": round(close_strength, 3),
    }


def _adjust_relaxed_first_board_probability(
    base_probability: float,
    *,
    strict_blockers: list[str],
    route: str,
) -> tuple[float, dict]:
    normalized_blockers = [str(item or "").strip() for item in strict_blockers if str(item or "").strip()]
    blocker_categories = {_classify_first_board_blocker(item) for item in normalized_blockers}
    category_penalty_map = {
        "缺少首波记忆": 0.04,
        "主线强度不足": 0.035,
        "量能不够": 0.03,
        "K线结构不够": 0.03,
        "其他约束": 0.02,
    }
    penalty = 0.08 + max(len(normalized_blockers) - 1, 0) * 0.022
    penalty += sum(category_penalty_map.get(category, 0.02) for category in blocker_categories)
    if route == "strict_stealth_scan":
        penalty += 0.02

    adjusted_probability = max(0.16, base_probability - penalty)
    sub_probability_penalty = min(0.24, penalty * 0.72 + 0.02)
    route_score_penalty = min(18.0, penalty * 65.0)
    return _clamp_probability(adjusted_probability), {
        "penalty": round(penalty, 3),
        "sub_probability_penalty": round(sub_probability_penalty, 3),
        "route_score_penalty": round(route_score_penalty, 3),
        "blocker_categories": sorted(blocker_categories),
    }


def _prediction_shape_watch_probability_cap(detail: dict) -> float:
    """限制仅用于召回的形态种子，避免观察池被展示成高胜率预测。"""
    is_prediction_seed = bool(detail.get("prediction_shape_seed"))
    is_washout_seed = bool(
        detail.get("sector_washout_reversal_watch")
        and is_prediction_seed
    )
    auction_cluster_confirmed = bool(detail.get("auction_cluster_confirmed"))
    auction_feed_complete = auction_context_complete(detail)
    if auction_cluster_confirmed and not auction_feed_complete:
        return 0.12
    if auction_cluster_confirmed and is_washout_seed:
        return 0.16
    if is_washout_seed:
        return 0.08
    return 0.0


def _should_keep_relaxed_first_board_watch(
    row: dict,
    *,
    bull_score: float,
    sector: dict,
    kline_context: dict,
    memory_features: dict,
    probability: float,
    probability_factors: dict,
    strict_blockers: list[str],
) -> bool:
    detail = row.get("detail") or {}
    blocker_categories = {_classify_first_board_blocker(item) for item in strict_blockers}
    route_score = _safe_float(probability_factors.get("route_score"))
    support_strength = _safe_float(detail.get("support_strength_score"))
    sector_strength = _safe_float(sector.get("strength_score"))
    sector_continuity = _calc_sector_continuity_score(sector)
    memory_score = _safe_float(memory_features.get("memory_score"))
    volume_ratio = _safe_float(detail.get("volume_ratio"))
    change_pct = _safe_float(detail.get("change_pct"))
    strict_confirmation_count = _safe_int(kline_context.get("strict_confirmation_count"))
    qualified_doji = bool(kline_context.get("qualified_doji_confirmation"))
    has_platform_contraction = bool(kline_context.get("has_platform_contraction"))
    has_volume_contraction = bool(kline_context.get("has_volume_contraction"))
    near_breakout = bool(kline_context.get("near_breakout"))
    platform_relaunch_meta = _build_platform_relaunch_signal_meta(kline_context, memory_features)
    support_squeeze_meta = _build_support_squeeze_signal_meta(kline_context, detail)
    limit_up_nearby_doji = bool(platform_relaunch_meta.get("limit_up_nearby_doji_confirmation"))
    has_recent_limit_up_event = bool(platform_relaunch_meta.get("has_recent_limit_up_event"))
    support_squeeze_ready = bool(support_squeeze_meta.get("support_squeeze_ready"))
    has_soft_platform_edge = near_breakout and (has_platform_contraction or has_volume_contraction or qualified_doji or limit_up_nearby_doji)
    event_types = {str(item or "").strip() for item in row.get("event_types") or []}

    has_memory_edge = memory_score >= 18.0 or has_recent_limit_up_event
    has_rotation_edge = (
        bool(sector.get("sector_low_position_rotation"))
        and _safe_float(sector.get("sector_rotation_score")) >= 56.0
        and support_strength >= 44.0
    )
    has_washout_edge = bool(
        sector.get("sector_washout_reversal_watch")
        and detail.get("prediction_shape_seed")
    )
    has_mainline_edge = (
        sector_strength >= 30.0
        or sector_continuity >= 0.12
        or bull_score >= 64.0
        or has_rotation_edge
        or has_washout_edge
    )
    has_news_edge = "news_catalyst" in event_types and _safe_float(detail.get("news_catalyst_score")) >= NEWS_CATALYST_MIN_SCORE
    has_auction_edge = "auction_surge" in event_types and _safe_float(detail.get("auction_strength_score")) >= AUCTION_SURGE_MIN_SCORE
    has_pre_board_edge = "pre_board_probe" in event_types and _safe_float(detail.get("pre_board_probe_score")) >= PRE_BOARD_PROBE_MIN_SCORE
    has_oversold_edge = "oversold_reversal" in event_types and _safe_float(detail.get("oversold_reversal_score")) >= OVERSOLD_REVERSAL_MIN_SCORE
    has_tape_edge = support_strength >= 42.0 or volume_ratio >= 0.8 or change_pct >= 0.5

    if probability < 0.18:
        return False
    if route_score < (28.0 if has_washout_edge else 36.0):
        return False
    if strict_confirmation_count < 1 and not (
        limit_up_nearby_doji
        or has_soft_platform_edge
        or support_squeeze_ready
        or has_washout_edge
    ):
        return False
    if len(blocker_categories) >= 4:
        return False
    if "其他约束" in blocker_categories and len(blocker_categories) >= 3:
        return False
    if not (
        has_memory_edge
        or has_mainline_edge
        or support_squeeze_ready
        or has_news_edge
        or has_auction_edge
        or has_pre_board_edge
        or has_oversold_edge
    ):
        return False
    if not (
        has_tape_edge
        or qualified_doji
        or limit_up_nearby_doji
        or has_soft_platform_edge
        or support_squeeze_ready
        or has_washout_edge
    ):
        return False
    return True


def _resolve_first_board_watch_bucket(route: str, event_types: list[str] | None = None) -> tuple[str, str, str]:
    normalized_route = str(route or "").strip()
    normalized_event_types = {str(item or "").strip() for item in (event_types or []) if str(item or "").strip()}

    if normalized_route in {
        "fresh_mainline_start",
        "fresh_relay_start",
        "support_squeeze_start",
        "news_catalyst_start",
        "auction_surge_start",
        "mainline_spread_start",
        "pre_board_probe_start",
        "oversold_reversal_start",
    }:
        bucket = normalized_route
    elif normalized_route == "platform_relaunch":
        bucket = "platform_relaunch"
    elif normalized_route in {"relay_fillup", "hot_primary", "fresh_hot_start"} or normalized_event_types.intersection({"capital", "breakthrough"}):
        bucket = "mainline_relay"
    elif normalized_route == "quiet_setup" or "stealth_setup" in normalized_event_types:
        bucket = "quiet_setup"
    else:
        bucket = "other"

    return (
        bucket,
        FIRST_BOARD_WATCH_BUCKET_LABELS.get(bucket, "观察补位"),
        FIRST_BOARD_WATCH_BUCKET_DESCRIPTIONS.get(bucket, "暂未形成明确的首板盘感分层，先留在观察补位区跟踪。"),
    )


def _build_first_board_main_uptrend_meta(
    *,
    probability: float,
    probability_factors: dict,
    memory_features: dict,
    support_strength: float,
    sector_strength: float,
) -> dict:
    sub_probabilities = probability_factors.get("sub_probabilities") or {}
    route_score = _safe_float(probability_factors.get("route_score"))
    memory_score = _safe_float(memory_features.get("memory_score"))
    theme_score = _safe_float(probability_factors.get("theme_score"))
    trigger_score = _safe_float(probability_factors.get("trigger_score"))
    platform_score = _safe_float(probability_factors.get("platform_score"))
    strict_confirmation_count = _safe_int(probability_factors.get("strict_confirmation_count"))
    breakout_probability = _safe_float(sub_probabilities.get("breakout_3d")) * 100.0
    first_limitup_probability = _safe_float(sub_probabilities.get("first_limitup_5d") or probability) * 100.0

    uptrend_score = _clamp_score(
        route_score * 0.36
        + breakout_probability * 0.17
        + first_limitup_probability * 0.14
        + memory_score * 0.10
        + theme_score * 0.08
        + trigger_score * 0.06
        + platform_score * 0.05
        + min(strict_confirmation_count, 4) / 4.0 * 100.0 * 0.02
        + min(max(support_strength, 0.0), 100.0) * 0.01
        + min(max(sector_strength, 0.0), 100.0) * 0.01
    )

    ready = (
        uptrend_score >= 56.0
        and breakout_probability >= 24.0
        and first_limitup_probability >= 22.0
        and (memory_score >= 18.0 or theme_score >= 52.0 or trigger_score >= 55.0)
    )
    if ready:
        label = "主升浪预备"
        reason = "主线、结构和点火质量已经接近主升浪启动阈值，接下来更看放量确认。"
    elif uptrend_score >= 48.0:
        label = "右侧待确认"
        reason = "已经靠近右侧启动区，但还缺一次更硬的量价确认或主线扩散。"
    else:
        label = "继续观察"
        reason = "盘感还没切到主升浪启动区，先跟踪主线和结构是否继续成熟。"

    return {
        "main_uptrend_score": round(uptrend_score, 2),
        "main_uptrend_label": label,
        "main_uptrend_reason": reason,
        "is_main_uptrend_ready": ready,
    }


def _build_first_board_watch_groups(
    candidates: list[dict],
    limit_per_group: int = 6,
    *,
    time_horizon: str = "watch",
) -> dict:
    watch_candidates = [item for item in candidates if str(item.get("time_horizon") or "") == time_horizon]
    ready_count = sum(1 for item in watch_candidates if bool(item.get("is_main_uptrend_ready")))
    grouped: dict[str, list[dict]] = {bucket: [] for bucket in FIRST_BOARD_WATCH_BUCKET_ORDER}
    for item in watch_candidates:
        bucket = str(item.get("watch_bucket") or "other")
        if bucket not in grouped:
            grouped[bucket] = []
        grouped[bucket].append(item)

    groups: list[dict] = []
    for bucket in FIRST_BOARD_WATCH_BUCKET_ORDER + tuple(
        item for item in grouped.keys() if item not in FIRST_BOARD_WATCH_BUCKET_ORDER
    ):
        bucket_items = grouped.get(bucket) or []
        if not bucket_items:
            continue
        bucket_items.sort(
            key=lambda item: (
                1 if bool(item.get("is_main_uptrend_ready")) else 0,
                _safe_float(item.get("main_uptrend_score")),
                _safe_float(item.get("probability")),
                _safe_float(item.get("route_score")),
            ),
            reverse=True,
        )
        groups.append(
            {
                "bucket": bucket,
                "label": str(
                    (bucket_items[0] or {}).get("watch_bucket_label")
                    or FIRST_BOARD_WATCH_BUCKET_LABELS.get(bucket, str(bucket or "观察补位"))
                ),
                "description": str(
                    (bucket_items[0] or {}).get("watch_bucket_reason")
                    or FIRST_BOARD_WATCH_BUCKET_DESCRIPTIONS.get(bucket, "暂未形成明确的首板盘感分层。")
                ),
                "count": len(bucket_items),
                "main_uptrend_ready_count": sum(1 for item in bucket_items if bool(item.get("is_main_uptrend_ready"))),
                "examples": bucket_items[:limit_per_group],
            }
        )

    dominant_group = max(groups, key=lambda item: item.get("count", 0), default=None)
    overview = {
        "watch_total": len(watch_candidates),
        "group_total": len(groups),
        "main_uptrend_ready_count": ready_count,
        "headline": "",
    }
    if dominant_group:
        if time_horizon == "pre_sprint":
            overview["headline"] = (
                f"当前准冲刺层以“{dominant_group['label']}”为主，共 {dominant_group['count']} 只，"
                f"已经更接近临盘要动区，重点看下一次放量和涨幅抬升。"
            )
        elif time_horizon == "weak_watch":
            overview["headline"] = (
                f"当前弱观察层以“{dominant_group['label']}”为主，共 {dominant_group['count']} 只，"
                f"其中 {ready_count} 只仍保留主升浪预备轮廓，先等止跌翻红。"
            )
        else:
            overview["headline"] = (
                f"当前首板梯队以“{dominant_group['label']}”为主，共 {dominant_group['count']} 只，"
                f"其中 {ready_count} 只已经接近主升浪启动区。"
            )
        overview["dominant_bucket"] = dominant_group["bucket"]
        overview["dominant_bucket_label"] = dominant_group["label"]

    return {
        "overview": overview,
        "groups": groups,
    }


def _platform_cycle_suffocation_target(cycle_type: str) -> float:
    normalized_cycle_type = str(cycle_type or "").strip()
    for config in PLATFORM_CYCLE_CONFIGS:
        if str(config.get("type") or "").strip() == normalized_cycle_type:
            return _safe_float(config.get("max_suffocation_ratio"), 0.74)
    return _safe_float((PLATFORM_CYCLE_CONFIGS[-1] if PLATFORM_CYCLE_CONFIGS else {}).get("max_suffocation_ratio"), 0.74)


def _is_limit_up_platform_pool_candidate(item: dict) -> bool:
    factors = item.get("probability_factors") or {}
    has_recent_limit_up_event = bool(factors.get("has_recent_limit_up_event"))
    limit_up_anchor_gap_pct = _safe_float(factors.get("limit_up_anchor_gap_pct"), 999.0)
    strict_confirmation_count = _safe_int(factors.get("strict_confirmation_count"))
    has_platform_contraction = bool(factors.get("has_platform_contraction"))
    has_volume_suffocation = bool(factors.get("has_volume_suffocation"))
    qualified_doji = bool(factors.get("qualified_doji_confirmation"))
    limit_up_nearby_doji = bool(factors.get("limit_up_nearby_doji_confirmation"))
    platform_relaunch_event_ready = bool(factors.get("platform_relaunch_event_ready"))
    candidate_route = str(item.get("candidate_route") or "")

    if not has_recent_limit_up_event:
        return False
    if platform_relaunch_event_ready or limit_up_nearby_doji or candidate_route == "platform_relaunch":
        return True
    return (
        limit_up_anchor_gap_pct <= PLATFORM_RELAUNCH_MAX_ANCHOR_GAP_PCT
        and strict_confirmation_count >= 2
        and (has_platform_contraction or has_volume_suffocation or qualified_doji)
    )


def _is_limit_up_platform_diagnostic_candidate(item: dict) -> bool:
    factors = item.get("probability_factors") or {}
    if not bool(factors.get("has_recent_limit_up_event")):
        return False

    limit_up_anchor_gap_pct = _safe_float(
        item.get("limit_up_anchor_gap_pct"),
        _safe_float(factors.get("limit_up_anchor_gap_pct"), 999.0),
    )
    signal_score = _safe_float(
        item.get("limit_up_nearby_signal_score"),
        _safe_float(factors.get("limit_up_nearby_signal_score")),
    )
    has_doji_confirmation = bool(factors.get("has_doji_confirmation")) or bool(factors.get("qualified_doji_confirmation"))
    has_volume_shape = bool(factors.get("has_volume_contraction")) or bool(factors.get("has_volume_suffocation"))
    return (
        limit_up_anchor_gap_pct <= PLATFORM_RELAUNCH_MAX_ANCHOR_GAP_PCT + 4.0
        or signal_score >= 38.0
        or has_doji_confirmation
        or has_volume_shape
        or str(item.get("candidate_route") or "") == "platform_relaunch"
    )


def _append_limit_up_platform_action(actions: list[dict], *, key: str, label: str, hint: str) -> None:
    normalized_key = str(key or "").strip()
    normalized_hint = str(hint or "").strip()
    if not normalized_key or not normalized_hint:
        return
    if any(str(item.get("key") or "") == normalized_key and str(item.get("hint") or "") == normalized_hint for item in actions):
        return
    actions.append({"key": normalized_key, "label": str(label or normalized_key), "hint": normalized_hint})


def _build_limit_up_platform_diagnostic_actions(entry: dict) -> list[dict]:
    actions: list[dict] = []
    anchor_gap_pct = _safe_float(entry.get("limit_up_anchor_gap_pct"), 999.0)
    anchor_target_pct = _safe_float(entry.get("limit_up_anchor_gap_target_pct"), PLATFORM_RELAUNCH_MAX_ANCHOR_GAP_PCT)
    limit_up_nearby_doji_confirmation = bool(entry.get("limit_up_nearby_doji_confirmation"))
    has_doji_confirmation = bool(entry.get("has_doji_confirmation"))
    qualified_doji_confirmation = bool(entry.get("qualified_doji_confirmation"))
    has_volume_suffocation = bool(entry.get("has_volume_suffocation"))
    has_volume_contraction = bool(entry.get("has_volume_contraction"))
    volume_suffocation_ratio = _safe_float(entry.get("volume_suffocation_ratio"))
    platform_cycle_type = str(entry.get("platform_cycle_type") or "")
    platform_cycle_label = str(entry.get("platform_cycle_label") or "平台")
    suffocation_target = _safe_float(entry.get("volume_suffocation_target_ratio"), _platform_cycle_suffocation_target(platform_cycle_type))

    if anchor_gap_pct > anchor_target_pct:
        _append_limit_up_platform_action(
            actions,
            key="limit_up_anchor_gap_pct",
            label="先贴回涨停锚点",
            hint=(
                f"离板锚点先收敛到 {anchor_target_pct:.1f}% 内，"
                f"当前还多 {max(anchor_gap_pct - anchor_target_pct, 0.0):.1f} 个点"
            ),
        )

    if not limit_up_nearby_doji_confirmation and not (qualified_doji_confirmation or has_doji_confirmation):
        hint = "还没等到板附近十字星，先等一根贴板缩量十字或确认小阳线。"
        _append_limit_up_platform_action(
            actions,
            key="limit_up_nearby_doji_confirmation",
            label="补板附近十字星",
            hint=hint,
        )

    if not has_volume_suffocation:
        if volume_suffocation_ratio > 0:
            hint = (
                f"{platform_cycle_label}量窒息比先压到 {suffocation_target:.2f} 内，"
                f"当前还高出 {max(volume_suffocation_ratio - suffocation_target, 0.0):.2f}"
            )
        elif has_volume_contraction:
            hint = f"{platform_cycle_label}已经在缩量，但还没到量窒息；再等 1-2 根缩量K 更合适。"
        else:
            hint = "量能还没缩到静默区，先等成交量继续收敛，再看弱转强。"
        _append_limit_up_platform_action(
            actions,
            key="volume_suffocation_ratio",
            label="补量窒息",
            hint=hint,
        )

    if not actions:
        _append_limit_up_platform_action(
            actions,
            key="fallback",
            label="继续等结构",
            hint="位置、十字星和量窒息三件套还没完全站稳，先继续观察。",
        )
    return actions[:3]


def _limit_up_platform_diagnostic_relevance(entry: dict) -> float:
    return (
        _safe_float(entry.get("limit_up_nearby_signal_score")) * 1.2
        + _safe_float(entry.get("route_score")) * 0.25
        + _safe_float(entry.get("probability")) * 35.0
        + (8.0 if str(entry.get("time_horizon") or "") == "sprint" else 0.0)
        - _safe_float(entry.get("limit_up_anchor_gap_pct")) * 2.2
    )


def _build_limit_up_platform_diagnostics_payload(
    candidates: list[dict],
    selected_candidates: list[dict],
    *,
    limit_per_group: int = 5,
) -> dict:
    selected_codes = {
        str(item.get("code") or "").strip()
        for item in selected_candidates
        if str(item.get("code") or "").strip()
    }
    diagnostics: list[dict] = []

    for raw_item in candidates:
        item = _annotate_first_board_timing(raw_item)
        code = str(item.get("code") or "").strip()
        if not code or code in selected_codes or not _is_limit_up_platform_diagnostic_candidate(item):
            continue

        factors = item.get("probability_factors") or {}
        limit_up_anchor_gap_pct = _safe_float(
            item.get("limit_up_anchor_gap_pct"),
            _safe_float(factors.get("limit_up_anchor_gap_pct"), 999.0),
        )
        limit_up_nearby_doji_confirmation = bool(
            item.get("limit_up_nearby_doji_confirmation")
            or factors.get("limit_up_nearby_doji_confirmation")
        )
        has_doji_confirmation = bool(factors.get("has_doji_confirmation"))
        qualified_doji_confirmation = bool(factors.get("qualified_doji_confirmation"))
        has_volume_suffocation = bool(factors.get("has_volume_suffocation"))
        has_volume_contraction = bool(factors.get("has_volume_contraction"))

        blockers: list[str] = []
        if limit_up_anchor_gap_pct > PLATFORM_RELAUNCH_MAX_ANCHOR_GAP_PCT:
            blockers.append("离板锚点仍偏远")
        if not limit_up_nearby_doji_confirmation and not (qualified_doji_confirmation or has_doji_confirmation):
            blockers.append("还差十字星确认")
        if not has_volume_suffocation:
            blockers.append("还差量窒息")
        if not blockers:
            continue

        primary_reason = next(
            (reason for reason in LIMIT_UP_PLATFORM_DIAGNOSTIC_REASON_ORDER if reason in blockers),
            blockers[0],
        )
        entry = {
            "code": code,
            "name": str(item.get("name") or "").strip(),
            "primary_reason": primary_reason,
            "blockers": blockers,
            "limit_up_anchor_gap_pct": round(limit_up_anchor_gap_pct, 2),
            "limit_up_anchor_gap_target_pct": round(PLATFORM_RELAUNCH_MAX_ANCHOR_GAP_PCT, 2),
            "limit_up_nearby_doji_confirmation": limit_up_nearby_doji_confirmation,
            "has_doji_confirmation": has_doji_confirmation,
            "qualified_doji_confirmation": qualified_doji_confirmation,
            "has_volume_suffocation": has_volume_suffocation,
            "has_volume_contraction": has_volume_contraction,
            "volume_suffocation_ratio": round(_safe_float(factors.get("volume_suffocation_ratio")), 3),
            "volume_suffocation_target_ratio": round(
                _platform_cycle_suffocation_target(str(factors.get("platform_cycle_type") or "")),
                3,
            ),
            "volume_shrink_ratio": round(_safe_float(factors.get("volume_shrink_ratio")), 3),
            "platform_cycle_type": str(factors.get("platform_cycle_type") or ""),
            "platform_cycle_label": str(factors.get("platform_cycle_label") or "平台"),
            "signal_summary": str(item.get("signal_summary") or ""),
            "primary_signal_reason": str(item.get("primary_reason") or ""),
            "secondary_signal_reason": str(item.get("secondary_reason") or ""),
            "time_horizon": str(item.get("time_horizon") or ""),
            "time_horizon_label": str(item.get("time_horizon_label") or ""),
            "candidate_route": str(item.get("candidate_route") or ""),
            "candidate_route_label": str(item.get("candidate_route_label") or ""),
            "probability": _safe_float(item.get("probability")),
            "route_score": _safe_float(item.get("route_score")),
            "limit_up_nearby_signal_score": _safe_float(
                item.get("limit_up_nearby_signal_score"),
                _safe_float(factors.get("limit_up_nearby_signal_score")),
            ),
            "latest_as_of": str(item.get("latest_as_of") or ""),
            "memory_score": _safe_float((item.get("memory_features") or {}).get("memory_score")),
            "memory_last_limit_up_date": str((item.get("memory_features") or {}).get("memory_last_limit_up_date") or ""),
        }
        entry["actionable_actions"] = _build_limit_up_platform_diagnostic_actions(entry)
        entry["actionable_suggestions"] = [str(action.get("hint") or "") for action in entry["actionable_actions"]]
        first_action = entry["actionable_actions"][0] if entry["actionable_actions"] else {}
        entry["next_focus_key"] = str(first_action.get("key") or "")
        entry["next_focus_label"] = str(first_action.get("label") or "")
        entry["next_threshold_hint"] = entry["actionable_suggestions"][0] if entry["actionable_suggestions"] else ""
        diagnostics.append(entry)

    if not diagnostics:
        return {
            "blocked_total": 0,
            "overview": {
                "headline": "当前没有明显卡在贴板横盘门槛外的样本，近期涨停股要么已经进池，要么还没走到这一步。",
                "dominant_reason": "",
                "dominant_reason_label": "",
                "dominant_count": 0,
                "dominant_share": 0.0,
            },
            "reason_summary": [],
            "blocked_reason_groups": [],
            "notes": [
                "仅统计最近真实涨停后 3-20 日内，且仍在首板候选/梯队里的个股",
                "只聚焦离板锚点、十字星确认和量窒息三类缺口",
            ],
        }

    total = len(diagnostics)
    counts: dict[str, int] = {}
    for entry in diagnostics:
        counts[entry["primary_reason"]] = counts.get(entry["primary_reason"], 0) + 1

    reason_summary = sorted(
        [
            {
                "reason": reason,
                "label": reason,
                "count": counts[reason],
                "share": round(counts[reason] / total, 4),
            }
            for reason in LIMIT_UP_PLATFORM_DIAGNOSTIC_REASON_ORDER
            if counts.get(reason)
        ],
        key=lambda item: (
            _safe_int(item.get("count")),
            -LIMIT_UP_PLATFORM_DIAGNOSTIC_REASON_ORDER.index(str(item.get("reason") or "")),
        ),
        reverse=True,
    )

    dominant = reason_summary[0] if reason_summary else {}
    dominant_reason = str(dominant.get("reason") or "")
    dominant_label = str(dominant.get("label") or dominant_reason)
    dominant_count = _safe_int(dominant.get("count"))
    dominant_share = dominant_count / total if total else 0.0
    if dominant_share >= 0.55:
        headline = (
            f"当前有 {total} 只近期涨停股没进“贴板横盘预备池”，"
            f"主要卡在“{dominant_label}”，占比 {dominant_share * 100:.1f}%。"
        )
    else:
        headline = (
            f"当前有 {total} 只近期涨停股还停在贴板池外，"
            f"最常见的缺口是“{dominant_label}”，共有 {dominant_count} 只。"
        )

    blocked_reason_groups: list[dict] = []
    for reason in LIMIT_UP_PLATFORM_DIAGNOSTIC_REASON_ORDER:
        reason_entries = [entry for entry in diagnostics if entry["primary_reason"] == reason]
        if not reason_entries:
            continue
        reason_entries.sort(
            key=lambda entry: (
                _limit_up_platform_diagnostic_relevance(entry),
                str(entry.get("code") or ""),
            ),
            reverse=True,
        )
        blocked_reason_groups.append(
            {
                "reason": reason,
                "label": reason,
                "count": len(reason_entries),
                "playbook": LIMIT_UP_PLATFORM_DIAGNOSTIC_PLAYBOOKS.get(reason, ()),
                "examples": reason_entries[:limit_per_group],
            }
        )

    return {
        "blocked_total": total,
        "overview": {
            "headline": headline,
            "dominant_reason": dominant_reason,
            "dominant_reason_label": dominant_label,
            "dominant_count": dominant_count,
            "dominant_share": round(dominant_share, 4),
        },
        "reason_summary": reason_summary,
        "blocked_reason_groups": blocked_reason_groups,
        "notes": [
            "仅统计最近真实涨停后 3-20 日内，且仍在首板候选/梯队里的个股",
            "只聚焦离板锚点、十字星确认和量窒息三类缺口",
        ],
    }


def _build_limit_up_platform_pool(candidates: list[dict], limit: int = 12) -> dict:
    annotated_candidates = [_annotate_first_board_timing(item) for item in candidates]
    selected = [item for item in annotated_candidates if _is_limit_up_platform_pool_candidate(item)]
    selected.sort(
        key=lambda item: (
            1 if bool(item.get("limit_up_nearby_doji_confirmation")) else 0,
            1 if str(item.get("time_horizon") or "") == "sprint" else 0,
            _safe_float(item.get("limit_up_nearby_signal_score")),
            -_safe_float(item.get("limit_up_anchor_gap_pct"), 999.0),
            _safe_float(item.get("probability")),
            _safe_float(item.get("route_score")),
            _safe_float(item.get("memory_features", {}).get("memory_score")),
        ),
        reverse=True,
    )

    preview = selected[:limit]
    doji_count = sum(1 for item in selected if bool(item.get("limit_up_nearby_doji_confirmation")))
    sprint_count = sum(1 for item in selected if str(item.get("time_horizon") or "") == "sprint")
    overview = {
        "pool_total": len(selected),
        "doji_count": doji_count,
        "sprint_count": sprint_count,
        "watch_count": max(len(selected) - sprint_count, 0),
        "headline": "",
    }
    if selected:
        overview["headline"] = (
            f"当前有 {len(selected)} 只“板后贴板横盘”预备股，"
            f"其中 {doji_count} 只已经命中涨停板附近十字星，"
            f"{sprint_count} 只进入首板冲刺窗口。"
        )
        leader = selected[0]
        overview["leader_code"] = str(leader.get("code") or "")
        overview["leader_name"] = str(leader.get("name") or "")
        overview["leader_pattern_label"] = str(
            leader.get("limit_up_nearby_pattern_label")
            or leader.get("candidate_route_label")
            or "板后贴板横盘"
        )

    return {
        "overview": overview,
        "candidates": preview,
        "selected": selected,
    }


def _resolve_first_board_strategy_lane(route: str, event_types: list[str] | None = None) -> tuple[str, str, str]:
    normalized_route = str(route or "").strip()
    normalized_events = {str(item or "").strip() for item in (event_types or []) if str(item or "").strip()}
    if normalized_route == "auction_surge_start" or "auction_surge" in normalized_events:
        return (
            "auction_surge_first_board",
            "竞价高开强攻",
            "由集合竞价/开盘抢筹驱动，重点看开盘后承接和回落幅度。",
        )
    if normalized_route == "news_catalyst_start" or "news_catalyst" in normalized_events:
        return (
            "news_catalyst_first_board",
            "消息催化首板",
            "由公告、资讯或产业事件驱动，必须看消息硬度、竞价反馈和同题材扩散确认。",
        )
    if normalized_route == "mainline_spread_start" or "mainline_spread" in normalized_events:
        return (
            "mainline_spread_first_board",
            "主线扩散补涨首板",
            "由主线早盘扩散和分支补涨驱动，重点看同方向首封密度和补涨位置。",
        )
    if normalized_route in {
        "platform_relaunch",
        "support_squeeze_start",
        "pre_board_probe_start",
        "oversold_reversal_start",
        "quiet_setup",
    }:
        return (
            "low_absorb_halfway_watch",
            "低吸/半路观察池",
            "保留原有形态和承接算法，只作为观察或半路确认，不与消息/竞价/扩散买点混排解释。",
        )
    return (
        "trend_relay_watch",
        "趋势/补涨观察池",
        "偏趋势预热或分支卡位，先观察主线持续性和量价确认。",
    )


def _build_first_board_candidate_item(
    *,
    code: str,
    name: str,
    probability: float,
    probability_factors: dict,
    setup_grade: str,
    setup_grade_display: str,
    signal_summary: str,
    primary_reason: str,
    secondary_reason: str,
    detail: dict,
    display_score: float,
    bull_row: dict,
    bull_score: float,
    sector: dict,
    sector_reason: str,
    event_types: list[str],
    memory_features: dict,
    kline_context: dict,
    risk_flags: list,
    strict_blockers: list[str],
    prediction_mode: str,
    latest_as_of: str,
    signal_state: dict,
    time_horizon: str | None = None,
    time_horizon_label: str | None = None,
    time_horizon_reason: str | None = None,
    keep_in_diagnostics: bool = False,
    trade_ready: bool = True,
    strict_gate_passed: bool = True,
) -> dict:
    confidence_level, confidence_score, confidence_label = _confidence_from_probability(probability)
    candidate_route = str(probability_factors.get("candidate_route") or "")
    main_probability_name = str(probability_factors.get("main_probability_name") or FIRST_BOARD_MAIN_PROBABILITY_NAME)
    watch_bucket, watch_bucket_label, watch_bucket_reason = _resolve_first_board_watch_bucket(candidate_route, event_types)
    strategy_lane, strategy_lane_label, strategy_lane_reason = _resolve_first_board_strategy_lane(candidate_route, event_types)
    main_uptrend_meta = _build_first_board_main_uptrend_meta(
        probability=probability,
        probability_factors=probability_factors,
        memory_features=memory_features,
        support_strength=_safe_float(detail.get("support_strength_score")),
        sector_strength=_safe_float(sector.get("strength_score")),
    )
    enriched_kline_context = {
        **kline_context,
        "has_recent_limit_up_event": bool(probability_factors.get("has_recent_limit_up_event")),
        "platform_relaunch_event_ready": bool(probability_factors.get("platform_relaunch_event_ready")),
        "limit_up_nearby_doji_confirmation": bool(probability_factors.get("limit_up_nearby_doji_confirmation")),
        "limit_up_anchor_gap_pct": round(_safe_float(probability_factors.get("limit_up_anchor_gap_pct")), 2),
        "limit_up_nearby_pattern_label": str(probability_factors.get("limit_up_nearby_pattern_label") or ""),
        "support_squeeze_ready": bool(probability_factors.get("support_squeeze_ready")),
        "support_volume_release_ready": bool(probability_factors.get("support_volume_release_ready")),
        "support_squeeze_pattern_label": str(probability_factors.get("support_squeeze_pattern_label") or ""),
        "support_zone_gap_pct": round(_safe_float(probability_factors.get("support_zone_gap_pct")), 2),
        "limit_probe_shape_label": str(probability_factors.get("limit_probe_shape_label") or ""),
        "limit_probe_shape_score": _safe_float(probability_factors.get("limit_probe_shape_score")),
        "limit_probe_shape_supportive": bool(probability_factors.get("limit_probe_shape_supportive")),
        "limit_probe_shape_failed": bool(probability_factors.get("limit_probe_shape_failed")),
        "pre_board_precursor_type": str(probability_factors.get("pre_board_precursor_type") or ""),
        "pre_board_precursor_label": str(probability_factors.get("pre_board_precursor_label") or ""),
        "momentum_shakeout_ready": bool(probability_factors.get("momentum_shakeout_ready")),
        "momentum_shakeout_score": _safe_float(probability_factors.get("momentum_shakeout_score")),
        "board_memory_reset_ready": bool(probability_factors.get("board_memory_reset_ready")),
        "board_memory_reset_score": _safe_float(probability_factors.get("board_memory_reset_score")),
        "board_memory_days_since_limit_up": _safe_int(probability_factors.get("board_memory_days_since_limit_up")),
        "board_memory_position_120": _safe_float(probability_factors.get("board_memory_position_120"), 1.0),
        "board_memory_volume_ratio_20": _safe_float(probability_factors.get("board_memory_volume_ratio_20")),
        "low_base_rotation_ready": bool(probability_factors.get("low_base_rotation_ready")),
        "low_base_rotation_score": _safe_float(probability_factors.get("low_base_rotation_score")),
        "low_base_position_120": _safe_float(probability_factors.get("low_base_position_120"), 1.0),
        "low_base_volume_ratio_5_20": _safe_float(probability_factors.get("low_base_volume_ratio_5_20")),
        "launch_profile_ready": bool(probability_factors.get("launch_profile_ready")),
        "launch_profile_score": _safe_float(probability_factors.get("launch_profile_score")),
        "launch_position_120": _safe_float(probability_factors.get("launch_position_120"), 1.0),
        "launch_return_20d": _safe_float(probability_factors.get("launch_return_20d")),
        "launch_volume_ratio_20": _safe_float(probability_factors.get("launch_volume_ratio_20")),
        "funding_preheat_ready": bool(probability_factors.get("funding_preheat_ready")),
        "funding_preheat_score": _safe_float(probability_factors.get("funding_preheat_score")),
        "primary_industry_ignition_ready": bool(probability_factors.get("primary_industry_ignition_ready")),
        "low_base_sector_ignition_ready": bool(probability_factors.get("low_base_sector_ignition_ready")),
        "low_base_sector_ignition_score": _safe_float(probability_factors.get("low_base_sector_ignition_score")),
        "prediction_shape_seed": bool(probability_factors.get("prediction_shape_seed")),
        "burst_pullback_restart_ready": bool(probability_factors.get("burst_pullback_restart_ready")),
        "burst_pullback_score": _safe_float(probability_factors.get("burst_pullback_score")),
        "burst_pullback_label": str(probability_factors.get("burst_pullback_label") or ""),
        "burst_date": str(probability_factors.get("burst_date") or ""),
        "burst_volume_ratio": _safe_float(probability_factors.get("burst_volume_ratio")),
        "burst_pullback_depth_pct": _safe_float(probability_factors.get("burst_pullback_depth_pct")),
        "burst_shrink_ratio": _safe_float(probability_factors.get("burst_shrink_ratio")),
        "burst_rebound_pct": _safe_float(probability_factors.get("burst_rebound_pct")),
        "burst_high_gap_pct": _safe_float(probability_factors.get("burst_high_gap_pct")),
        "burst_restart_days": _safe_int(probability_factors.get("burst_restart_days")),
        "burst_restart_volume_ratio": _safe_float(probability_factors.get("burst_restart_volume_ratio")),
    }
    candidate = {
        "code": code,
        "name": name,
        "target_board": 1,
        "target_label": "冲首板",
        "probability": probability,
        "main_probability_name": main_probability_name,
        "sub_probabilities": probability_factors.get("sub_probabilities") or {},
        "confidence": confidence_score,
        "confidence_level": confidence_level,
        "confidence_label": confidence_label,
        "setup_grade": setup_grade,
        "setup_grade_display": setup_grade_display,
        "signal_summary": signal_summary,
        "primary_reason": primary_reason,
        "secondary_reason": secondary_reason,
        "current_price": _safe_float(detail.get("price")),
        "change_pct": _safe_float(detail.get("change_pct")),
        "turnover": _safe_float(detail.get("turnover")),
        "volume_ratio": _safe_float(detail.get("volume_ratio")),
        "main_net_inflow": _safe_float(detail.get("main_net_inflow")),
        "main_net_inflow_pct": _safe_float(detail.get("main_net_inflow_pct")),
        "funding_main_inflow_pct_3d": _safe_float(probability_factors.get("funding_main_inflow_pct_3d")),
        "funding_positive_days_3d": _safe_int(probability_factors.get("funding_positive_days_3d")),
        "funding_preheat_ready": bool(probability_factors.get("funding_preheat_ready")),
        "funding_preheat_score": _safe_float(probability_factors.get("funding_preheat_score")),
        "primary_industry_name": str(probability_factors.get("primary_industry_name") or ""),
        "primary_industry_ignition_ready": bool(probability_factors.get("primary_industry_ignition_ready")),
        "primary_industry_ignition_score": _safe_float(probability_factors.get("primary_industry_ignition_score")),
        "low_base_sector_ignition_ready": bool(probability_factors.get("low_base_sector_ignition_ready")),
        "low_base_sector_ignition_confirmed": bool(probability_factors.get("low_base_sector_ignition_confirmed")),
        "low_base_sector_ignition_score": _safe_float(probability_factors.get("low_base_sector_ignition_score")),
        "support_strength_score": _safe_float(detail.get("support_strength_score")),
        "news_catalyst_score": _safe_float(probability_factors.get("news_catalyst_score")),
        "news_title": str(probability_factors.get("news_title") or detail.get("news_title") or ""),
        "news_source": str(probability_factors.get("news_source") or detail.get("news_source") or ""),
        "auction_strength_score": _safe_float(probability_factors.get("auction_strength_score")),
        "auction_open_change": _safe_float(probability_factors.get("auction_open_change")),
        "auction_volume_ratio": _safe_float(probability_factors.get("auction_volume_ratio")),
        "auction_amount": _safe_float(probability_factors.get("auction_amount")),
        "pre_board_probe_score": _safe_float(probability_factors.get("pre_board_probe_score")),
        "oversold_reversal_score": _safe_float(probability_factors.get("oversold_reversal_score")),
        "intraday_high_pct": _safe_float(probability_factors.get("intraday_high_pct")),
        "upper_gap_pct": _safe_float(probability_factors.get("upper_gap_pct")),
        "lower_gap_pct": _safe_float(probability_factors.get("lower_gap_pct")),
        "bull_score": bull_score,
        "bull_level": str(bull_row.get("level") or ""),
        "display_score": display_score,
        "event_types": list(event_types),
        "sector_name": sector_reason,
        "sector_strength_score": _safe_float(sector.get("strength_score")),
        "sector_limit_up_count": _safe_int(sector.get("limit_up_count")),
        "sector_consecutive_days": _safe_int(sector.get("consecutive_days")),
        "sector_rotation_score": _safe_float(probability_factors.get("sector_rotation_score")),
        "sector_strength_delta": _safe_float(probability_factors.get("sector_strength_delta")),
        "sector_limit_up_delta": _safe_float(probability_factors.get("sector_limit_up_delta")),
        "sector_low_position_rotation": bool(probability_factors.get("sector_low_position_rotation")),
        "sector_crowded_stale_theme": bool(probability_factors.get("sector_crowded_stale_theme")),
        "sector_rotation_label": str(probability_factors.get("sector_rotation_label") or ""),
        "candidate_route": candidate_route,
        "candidate_route_label": str(
            probability_factors.get("candidate_route_label") or FIRST_BOARD_ROUTE_LABELS.get(candidate_route, candidate_route)
        ),
        "strategy_lane": strategy_lane,
        "strategy_lane_label": strategy_lane_label,
        "strategy_lane_reason": strategy_lane_reason,
        "watch_bucket": watch_bucket,
        "watch_bucket_label": watch_bucket_label,
        "watch_bucket_reason": watch_bucket_reason,
        "route_score": _safe_float(probability_factors.get("route_score")),
        "has_recent_limit_up_event": bool(probability_factors.get("has_recent_limit_up_event")),
        "platform_relaunch_event_ready": bool(probability_factors.get("platform_relaunch_event_ready")),
        "limit_up_anchor_gap_pct": _safe_float(probability_factors.get("limit_up_anchor_gap_pct")),
        "limit_up_nearby_doji_confirmation": bool(probability_factors.get("limit_up_nearby_doji_confirmation")),
        "limit_up_nearby_signal_score": _safe_float(probability_factors.get("limit_up_nearby_signal_score")),
        "limit_up_nearby_pattern_label": str(probability_factors.get("limit_up_nearby_pattern_label") or ""),
        "support_squeeze_ready": bool(probability_factors.get("support_squeeze_ready")),
        "support_volume_release_ready": bool(probability_factors.get("support_volume_release_ready")),
        "support_squeeze_signal_score": _safe_float(probability_factors.get("support_squeeze_signal_score")),
        "support_squeeze_pattern_label": str(probability_factors.get("support_squeeze_pattern_label") or ""),
        "support_zone_gap_pct": _safe_float(probability_factors.get("support_zone_gap_pct")),
        "support_hold_days": _safe_int(probability_factors.get("support_hold_days")),
        "limit_probe_shape_label": str(probability_factors.get("limit_probe_shape_label") or ""),
        "limit_probe_shape_score": _safe_float(probability_factors.get("limit_probe_shape_score")),
        "limit_probe_shape_supportive": bool(probability_factors.get("limit_probe_shape_supportive")),
        "limit_probe_shape_failed": bool(probability_factors.get("limit_probe_shape_failed")),
        "pre_board_precursor_type": str(probability_factors.get("pre_board_precursor_type") or ""),
        "pre_board_precursor_label": str(probability_factors.get("pre_board_precursor_label") or ""),
        "momentum_shakeout_ready": bool(probability_factors.get("momentum_shakeout_ready")),
        "momentum_shakeout_score": _safe_float(probability_factors.get("momentum_shakeout_score")),
        "prediction_shape_seed": bool(probability_factors.get("prediction_shape_seed")),
        "burst_pullback_restart_ready": bool(probability_factors.get("burst_pullback_restart_ready")),
        "burst_pullback_score": _safe_float(probability_factors.get("burst_pullback_score")),
        "burst_pullback_label": str(probability_factors.get("burst_pullback_label") or ""),
        "burst_date": str(probability_factors.get("burst_date") or ""),
        "burst_volume_ratio": _safe_float(probability_factors.get("burst_volume_ratio")),
        "burst_pullback_depth_pct": _safe_float(probability_factors.get("burst_pullback_depth_pct")),
        "burst_shrink_ratio": _safe_float(probability_factors.get("burst_shrink_ratio")),
        "burst_rebound_pct": _safe_float(probability_factors.get("burst_rebound_pct")),
        "burst_high_gap_pct": _safe_float(probability_factors.get("burst_high_gap_pct")),
        "burst_restart_days": _safe_int(probability_factors.get("burst_restart_days")),
        "burst_restart_volume_ratio": _safe_float(probability_factors.get("burst_restart_volume_ratio")),
        "memory_features": memory_features,
        "probability_factors": probability_factors,
        "kline_confirmation": enriched_kline_context,
        "risk_flags": risk_flags,
        "strict_blockers": strict_blockers,
        "prediction_mode": prediction_mode,
        "latest_as_of": latest_as_of,
        "trade_ready": trade_ready,
        "strict_gate_passed": strict_gate_passed,
        **main_uptrend_meta,
        **signal_state,
    }
    if time_horizon:
        candidate["time_horizon"] = time_horizon
    if time_horizon_label:
        candidate["time_horizon_label"] = time_horizon_label
    if time_horizon_reason:
        candidate["time_horizon_reason"] = time_horizon_reason
    if keep_in_diagnostics:
        candidate["keep_in_diagnostics"] = True
    return candidate


def _resolve_first_board_display_reasoning(
    *,
    probability_factors: dict,
    signal_summary: str,
    primary_reason: str,
    secondary_reason: str,
    sector_reason: str = "",
    blocker_reason: str = "",
) -> tuple[str, str, str]:
    route = str(probability_factors.get("candidate_route") or "")
    normalized_secondary_parts = [part.strip() for part in str(secondary_reason or "").split(" · ") if part and part.strip()]
    sector_reason = str(sector_reason or "").strip()

    if route != "support_squeeze_start":
        return signal_summary, primary_reason, secondary_reason

    support_pattern_label = str(probability_factors.get("support_squeeze_pattern_label") or "低位支撑首波")
    support_zone_gap_pct = _safe_float(probability_factors.get("support_zone_gap_pct"), 999.0)
    support_hold_days = _safe_int(probability_factors.get("support_hold_days"))
    support_rebound_count = _safe_int(probability_factors.get("support_rebound_count"))

    support_notes: list[str] = [support_pattern_label]
    if support_zone_gap_pct < 999.0:
        support_notes.append(f"距支撑位{support_zone_gap_pct:.1f}%")
    if support_hold_days > 0:
        support_notes.append(f"支撑稳住{support_hold_days}天")
    if support_rebound_count > 0:
        support_notes.append(f"回弹确认{support_rebound_count}次")
    if blocker_reason:
        support_notes.insert(0, blocker_reason)

    merged_secondary_parts = [*support_notes, *normalized_secondary_parts]
    if sector_reason:
        merged_secondary_parts.append(sector_reason)

    deduped_secondary_parts: list[str] = []
    for item in merged_secondary_parts:
        if item and item not in deduped_secondary_parts:
            deduped_secondary_parts.append(item)

    return (
        f"{support_pattern_label} / 首波预备",
        support_pattern_label,
        " · ".join(deduped_secondary_parts),
    )


def _build_support_squeeze_last_mile_guidance(item: dict, *, stage: str) -> dict:
    factors = item.get("probability_factors") or {}
    signal_status = str(item.get("signal_status") or "")
    probability = _safe_float(item.get("probability"))
    breakout_probability = _safe_float((item.get("sub_probabilities") or {}).get("breakout_3d"))
    change_pct = _safe_float(item.get("change_pct"))
    volume_ratio = _safe_float(item.get("volume_ratio"))
    support_strength = _safe_float(item.get("support_strength_score"))
    support_zone_gap_pct = _safe_float(item.get("support_zone_gap_pct"), _safe_float(factors.get("support_zone_gap_pct"), 999.0))
    support_hold_days = _safe_int(item.get("support_hold_days"), _safe_int(factors.get("support_hold_days")))
    strict_confirmation_count = _safe_int(factors.get("strict_confirmation_count"))
    qualified_doji = bool(factors.get("qualified_doji_confirmation"))
    support_volume_release_ready = bool(item.get("support_volume_release_ready") or factors.get("support_volume_release_ready"))
    support_pattern_label = str(item.get("support_squeeze_pattern_label") or factors.get("support_squeeze_pattern_label") or "")

    suggestions: list[str] = []
    gap_label = "临门一脚"

    if stage == "support_squeeze_sprint":
        gap_label = "差封板时机"
        sprint_change_target = 1.4 if support_volume_release_ready else 1.2
        if change_pct < sprint_change_target:
            _append_first_board_suggestion(
                suggestions,
                f"差封板时机：日内涨幅先抬到 {sprint_change_target:.1f}%+，当前还差 {max(sprint_change_target - change_pct, 0.0):.1f} 个点",
            )
        if support_volume_release_ready and volume_ratio < SUPPORT_VOLUME_RELEASE_MIN_RATIO:
            _append_first_board_suggestion(
                suggestions,
                f"放量启动还差一口气：量比至少回到 {SUPPORT_VOLUME_RELEASE_MIN_RATIO:.2f}+，当前还差 {max(SUPPORT_VOLUME_RELEASE_MIN_RATIO - volume_ratio, 0.0):.2f}",
            )
        elif not support_volume_release_ready and volume_ratio < 0.85:
            _append_first_board_suggestion(
                suggestions,
                f"放量点火还差一口气：量比至少回到 0.85+，当前还差 {max(0.85 - volume_ratio, 0.0):.2f}",
            )
        if support_strength < 50.0:
            _append_first_board_suggestion(
                suggestions,
                f"承接还要更硬：承接分最好先回到 50+，当前还差 {max(50.0 - support_strength, 0.0):.1f} 分",
            )
        if signal_status != PROMOTION_SIGNAL_STATUS_CLOSE_CONFIRMED:
            _append_first_board_suggestion(suggestions, "盘中形态还没收盘，先等收盘确认再看是否直接点火")
        if not suggestions:
            _append_first_board_suggestion(
                suggestions,
                f"差封板时机：{support_pattern_label or '支撑位首波'}已经稳住，下一步重点看首次放量上攻把涨幅推到点火区",
            )
    elif stage == "support_squeeze_weak_watch":
        gap_label = "先止跌翻红"
        if change_pct < 0.0:
            _append_first_board_suggestion(
                suggestions,
                f"先止跌翻红：日内涨幅至少先回到 0.0% 以上，当前还差 {max(0.0 - change_pct, 0.0):.1f} 个点",
            )
        if support_strength < 42.0:
            _append_first_board_suggestion(
                suggestions,
                f"承接还要稳一点：承接分至少回到 42+，当前还差 {max(42.0 - support_strength, 0.0):.1f} 分",
            )
        if strict_confirmation_count < 4 or not qualified_doji:
            _append_first_board_suggestion(
                suggestions,
                "确认K还不够：最好先补一根止跌翻红的确认小阳/十字星，再回观察预备层会更稳",
            )
        if signal_status != PROMOTION_SIGNAL_STATUS_CLOSE_CONFIRMED:
            _append_first_board_suggestion(suggestions, "盘中形态还没收盘，先等收盘确认是不是止跌翻红")
        if not suggestions:
            _append_first_board_suggestion(
                suggestions,
                f"{support_pattern_label or '支撑区'}还在，但今天偏弱，先等止跌翻红后再重新评估是否回观察预备",
            )
    else:
        watch_change_target = SUPPORT_VOLUME_RELEASE_SPRINT_MIN_CHANGE_PCT if support_volume_release_ready else 0.6
        if change_pct < watch_change_target:
            gap_label = "差涨幅"
            _append_first_board_suggestion(
                suggestions,
                f"差涨幅：日内涨幅至少回到 {watch_change_target:.1f}%+，当前还差 {max(watch_change_target - change_pct, 0.0):.1f} 个点",
            )
        if support_volume_release_ready and volume_ratio < SUPPORT_VOLUME_RELEASE_MIN_RATIO:
            if gap_label == "临门一脚":
                gap_label = "差启动量能"
            _append_first_board_suggestion(
                suggestions,
                f"差启动量能：量比至少回到 {SUPPORT_VOLUME_RELEASE_MIN_RATIO:.2f}+，当前还差 {max(SUPPORT_VOLUME_RELEASE_MIN_RATIO - volume_ratio, 0.0):.2f}",
            )
        if support_strength < 42.0:
            if gap_label == "临门一脚":
                gap_label = "差承接"
            _append_first_board_suggestion(
                suggestions,
                f"差承接：承接分至少回到 42+，当前还差 {max(42.0 - support_strength, 0.0):.1f} 分",
            )
        if strict_confirmation_count < 4 or not qualified_doji:
            if gap_label == "临门一脚":
                gap_label = "差确认K"
            if qualified_doji:
                _append_first_board_suggestion(
                    suggestions,
                    f"差确认K：结构确认项至少补到 4 项，当前还差 {max(4 - strict_confirmation_count, 0)} 项",
                )
            else:
                _append_first_board_suggestion(
                    suggestions,
                    "差确认K：最好补一根站稳支撑位的确认小阳/十字星，再进冲刺层会更稳",
                )
        if not support_volume_release_ready and volume_ratio > 1.0:
            _append_first_board_suggestion(
                suggestions,
                f"量能还可以再收一口：量比最好先压回 1.00 内，当前高出 {max(volume_ratio - 1.0, 0.0):.2f}",
            )
        if support_zone_gap_pct > 2.6:
            _append_first_board_suggestion(
                suggestions,
                f"位置还可以再贴近一点：距支撑位最好压到 2.6% 内，当前高出 {max(support_zone_gap_pct - 2.6, 0.0):.1f} 个点",
            )
        if signal_status != PROMOTION_SIGNAL_STATUS_CLOSE_CONFIRMED:
            _append_first_board_suggestion(suggestions, "盘中形态还没收盘，先等收盘确认再评估是否升格冲刺")
        if not suggestions:
            gap_label = "差启动时机"
            _append_first_board_suggestion(
                suggestions,
                f"{support_pattern_label or '支撑区'}已经稳住 {support_hold_days} 天，当前更像等待下一次弱转强的启动时机",
            )

    if breakout_probability < 0.25 and stage != "support_squeeze_sprint":
        _append_first_board_suggestion(
            suggestions,
            f"3日突破概率还在蓄力：当前 {breakout_probability * 100:.1f}%，先等右侧动能继续抬升",
        )
    if probability < 0.3 and stage == "support_squeeze_watch":
        _append_first_board_suggestion(
            suggestions,
            f"5日首板概率还在观察区：当前 {probability * 100:.1f}%，先补最靠前的一项缺口再看升格",
        )

    return {
        "support_squeeze_gap_label": gap_label,
        "next_threshold_hint": suggestions[0] if suggestions else "",
        "actionable_suggestions": suggestions,
    }


def _is_support_squeeze_sprint_candidate(item: dict) -> bool:
    probability = _safe_float(item.get("probability"))
    route_score = _safe_float(item.get("route_score"))
    sub_probabilities = item.get("sub_probabilities") or {}
    breakout_probability = _safe_float(sub_probabilities.get("breakout_3d"))
    support_strength = _safe_float(item.get("support_strength_score"))
    sector_strength = _safe_float(item.get("sector_strength_score"))
    change_pct = _safe_float(item.get("change_pct"))
    volume_ratio = _safe_float(item.get("volume_ratio"))
    factors = item.get("probability_factors") or {}
    support_squeeze_ready = bool(item.get("support_squeeze_ready") or factors.get("support_squeeze_ready"))
    support_volume_release_ready = bool(item.get("support_volume_release_ready") or factors.get("support_volume_release_ready"))
    support_zone_gap_pct = _safe_float(item.get("support_zone_gap_pct"), _safe_float(factors.get("support_zone_gap_pct"), 999.0))
    support_hold_days = _safe_int(item.get("support_hold_days"), _safe_int(factors.get("support_hold_days")))
    strict_confirmation_count = _safe_int(factors.get("strict_confirmation_count"))
    qualified_doji = bool(factors.get("qualified_doji_confirmation"))

    if support_volume_release_ready:
        return (
            support_squeeze_ready
            and (probability >= 0.32 or breakout_probability >= 0.24 or route_score >= 58.0)
            and (support_strength >= 46.0 or sector_strength >= 42.0)
            and SUPPORT_VOLUME_RELEASE_SPRINT_MIN_CHANGE_PCT <= change_pct <= 5.2
            and SUPPORT_VOLUME_RELEASE_MIN_RATIO <= volume_ratio <= 2.4
            and support_zone_gap_pct <= 2.8
            and support_hold_days >= 3
            and strict_confirmation_count >= 2
            and qualified_doji
        )

    return (
        support_squeeze_ready
        and (probability >= 0.34 or breakout_probability >= 0.26 or route_score >= 61.0)
        and (support_strength >= 42.0 or sector_strength >= 45.0)
        and 0.8 <= change_pct <= 3.8
        and 0.7 <= volume_ratio <= 1.15
        and support_zone_gap_pct <= 2.8
        and support_hold_days >= 4
        and strict_confirmation_count >= 3
        and qualified_doji
    )


def _is_platform_relaunch_sprint_candidate(item: dict) -> bool:
    probability = _safe_float(item.get("probability"))
    route_score = _safe_float(item.get("route_score"))
    sub_probabilities = item.get("sub_probabilities") or {}
    breakout_probability = _safe_float(sub_probabilities.get("breakout_3d"))
    support_strength = _safe_float(item.get("support_strength_score"))
    sector_strength = _safe_float(item.get("sector_strength_score"))
    change_pct = _safe_float(item.get("change_pct"))
    volume_ratio = _safe_float(item.get("volume_ratio"))
    signal_status = str(item.get("signal_status") or "")
    factors = item.get("probability_factors") or {}
    strict_confirmation_count = _safe_int(factors.get("strict_confirmation_count"))
    qualified_doji = bool(factors.get("qualified_doji_confirmation"))
    limit_up_nearby_doji = bool(item.get("limit_up_nearby_doji_confirmation") or factors.get("limit_up_nearby_doji_confirmation"))
    platform_relaunch_event_ready = bool(item.get("platform_relaunch_event_ready") or factors.get("platform_relaunch_event_ready"))
    limit_up_anchor_gap_pct = _safe_float(item.get("limit_up_anchor_gap_pct"), _safe_float(factors.get("limit_up_anchor_gap_pct"), 999.0))

    return (
        (probability >= 0.33 or breakout_probability >= 0.25 or route_score >= 57.0)
        and (support_strength >= 45.0 or sector_strength >= 42.0)
        and 0.8 <= change_pct <= 5.6
        and 0.85 <= volume_ratio <= 2.4
        and strict_confirmation_count >= 2
        and (platform_relaunch_event_ready or limit_up_nearby_doji)
        and limit_up_anchor_gap_pct <= 3.8
        and (qualified_doji or signal_status == PROMOTION_SIGNAL_STATUS_CLOSE_CONFIRMED)
    )


def _is_hot_primary_sprint_candidate(item: dict) -> bool:
    probability = _safe_float(item.get("probability"))
    route_score = _safe_float(item.get("route_score"))
    sub_probabilities = item.get("sub_probabilities") or {}
    breakout_probability = _safe_float(sub_probabilities.get("breakout_3d"))
    support_strength = _safe_float(item.get("support_strength_score"))
    sector_strength = _safe_float(item.get("sector_strength_score"))
    change_pct = _safe_float(item.get("change_pct"))
    volume_ratio = _safe_float(item.get("volume_ratio"))
    main_inflow_pct = _safe_float(item.get("main_net_inflow_pct"))
    signal_status = str(item.get("signal_status") or "")
    factors = item.get("probability_factors") or {}
    strict_confirmation_count = _safe_int(factors.get("strict_confirmation_count"))
    qualified_doji = bool(factors.get("qualified_doji_confirmation"))
    trend_acceleration_ready = bool(factors.get("trend_acceleration_ready"))

    return (
        (probability >= 0.4 or breakout_probability >= 0.28 or route_score >= 60.0)
        and (support_strength >= 52.0 or sector_strength >= 46.0)
        and 1.2 <= change_pct <= 7.2
        and 1.0 <= volume_ratio <= 3.6
        and main_inflow_pct >= 2.0
        and strict_confirmation_count >= 2
        and (qualified_doji or trend_acceleration_ready or signal_status == PROMOTION_SIGNAL_STATUS_CLOSE_CONFIRMED)
    )


def _is_fresh_hot_sprint_candidate(item: dict) -> bool:
    candidate_route = str(item.get("candidate_route") or "")
    probability = _safe_float(item.get("probability"))
    route_score = _safe_float(item.get("route_score"))
    sub_probabilities = item.get("sub_probabilities") or {}
    breakout_probability = _safe_float(sub_probabilities.get("breakout_3d"))
    support_strength = _safe_float(item.get("support_strength_score"))
    sector_strength = _safe_float(item.get("sector_strength_score"))
    change_pct = _safe_float(item.get("change_pct"))
    volume_ratio = _safe_float(item.get("volume_ratio"))
    main_inflow_pct = _safe_float(item.get("main_net_inflow_pct"))
    signal_status = str(item.get("signal_status") or "")
    factors = item.get("probability_factors") or {}
    strict_confirmation_count = _safe_int(factors.get("strict_confirmation_count"))
    qualified_doji = bool(factors.get("qualified_doji_confirmation"))
    trend_acceleration_ready = bool(factors.get("trend_acceleration_ready"))

    min_probability = 0.38 if candidate_route == "fresh_mainline_start" else 0.42
    min_breakout_probability = 0.28 if candidate_route == "fresh_mainline_start" else 0.3
    min_route_score = 59.0 if candidate_route == "fresh_mainline_start" else 63.0
    min_sector_strength = 44.0 if candidate_route == "fresh_mainline_start" else 36.0
    min_support_strength = 48.0 if candidate_route == "fresh_mainline_start" else 50.0
    min_change_pct = 1.0 if candidate_route == "fresh_mainline_start" else 1.2

    return (
        (probability >= min_probability or breakout_probability >= min_breakout_probability or route_score >= min_route_score)
        and (support_strength >= min_support_strength or sector_strength >= min_sector_strength)
        and min_change_pct <= change_pct <= 6.8
        and 1.0 <= volume_ratio <= 3.2
        and main_inflow_pct >= 1.4
        and strict_confirmation_count >= 2
        and (qualified_doji or trend_acceleration_ready or signal_status == PROMOTION_SIGNAL_STATUS_CLOSE_CONFIRMED)
    )


def _is_news_catalyst_sprint_candidate(item: dict) -> bool:
    probability = _safe_float(item.get("probability"))
    route_score = _safe_float(item.get("route_score"))
    support_strength = _safe_float(item.get("support_strength_score"))
    sector_strength = _safe_float(item.get("sector_strength_score"))
    change_pct = _safe_float(item.get("change_pct"))
    volume_ratio = _safe_float(item.get("volume_ratio"))
    main_inflow_pct = _safe_float(item.get("main_net_inflow_pct"))
    factors = item.get("probability_factors") or {}
    catalyst_score = _safe_float(factors.get("news_catalyst_score"))
    return (
        (probability >= 0.20 or route_score >= 52.0 or catalyst_score >= 68.0)
        and catalyst_score >= NEWS_CATALYST_MIN_SCORE
        and (support_strength >= 42.0 or sector_strength >= 35.0)
        and -1.0 <= change_pct <= 9.2
        and 0.55 <= volume_ratio <= 4.8
        and main_inflow_pct >= -3.5
    )


def _is_auction_surge_sprint_candidate(item: dict) -> bool:
    probability = _safe_float(item.get("probability"))
    route_score = _safe_float(item.get("route_score"))
    support_strength = _safe_float(item.get("support_strength_score"))
    sector_strength = _safe_float(item.get("sector_strength_score"))
    change_pct = _safe_float(item.get("change_pct"))
    volume_ratio = _safe_float(item.get("volume_ratio"))
    factors = item.get("probability_factors") or {}
    auction_score = _safe_float(factors.get("auction_strength_score"))
    open_change = _safe_float(factors.get("auction_open_change"), _safe_float((item.get("detail") or {}).get("auction_open_change")))
    auction_feed_complete = auction_context_complete(factors)
    return (
        (probability >= 0.18 or route_score >= 50.0 or auction_score >= 60.0)
        and auction_score >= AUCTION_SURGE_MIN_SCORE
        and AUCTION_SURGE_MIN_OPEN_CHANGE <= open_change < AUCTION_SURGE_MAX_OPEN_CHANGE
        and auction_feed_complete
        and (support_strength >= 42.0 or sector_strength >= 30.0)
        and 0.0 <= change_pct <= 8.8
        and 0.55 <= volume_ratio <= 5.2
    )


def _is_mainline_spread_sprint_candidate(item: dict) -> bool:
    probability = _safe_float(item.get("probability"))
    route_score = _safe_float(item.get("route_score"))
    support_strength = _safe_float(item.get("support_strength_score"))
    sector_strength = _safe_float(item.get("sector_strength_score"))
    change_pct = _safe_float(item.get("change_pct"))
    volume_ratio = _safe_float(item.get("volume_ratio"))
    factors = item.get("probability_factors") or {}
    sector_continuity = _safe_float(factors.get("sector_continuity_score"))
    strict_confirmation_count = _safe_int(factors.get("strict_confirmation_count"))
    broad_rotation_cluster = bool(factors.get("broad_rotation_cluster_setup"))
    broad_rotation_ok = (
        bool(factors.get("market_broad_first_board_overflow"))
        and (sector_strength >= BROAD_ROTATION_MAINLINE_MIN_SECTOR_STRENGTH or broad_rotation_cluster)
        and (sector_continuity >= BROAD_ROTATION_MAINLINE_MIN_SECTOR_CONTINUITY or broad_rotation_cluster)
        and (
            bool(factors.get("sector_low_position_rotation"))
            or _safe_float(factors.get("sector_limit_up_delta")) >= 2.0
            or _safe_float(factors.get("broad_rotation_route_points")) >= 1.4
            or broad_rotation_cluster
        )
        and (support_strength >= (36.0 if broad_rotation_cluster else 42.0) or _safe_float(item.get("main_net_inflow_pct")) >= 1.5)
    )
    return (
        (
            probability >= 0.18
            or route_score >= 50.0
            or (
                broad_rotation_cluster
                and route_score >= 45.0
                and support_strength >= 50.0
                and strict_confirmation_count >= 2
            )
        )
        and (
            (
                sector_strength >= MAINLINE_SPREAD_MIN_SECTOR_STRENGTH
                and sector_continuity >= MAINLINE_SPREAD_MIN_SECTOR_CONTINUITY
            )
            or broad_rotation_ok
        )
        and (support_strength >= 46.0 or _safe_float(item.get("main_net_inflow_pct")) >= 2.0 or broad_rotation_ok)
        and (-6.5 if broad_rotation_cluster else -2.5 if broad_rotation_ok else MAINLINE_SPREAD_MIN_CHANGE_PCT) <= change_pct <= 8.0
        and (0.35 if broad_rotation_cluster else 0.55 if broad_rotation_ok else HOT_MAINLINE_RELAY_MIN_VOLUME_RATIO) <= volume_ratio <= 6.8
    )


def _is_pre_board_probe_sprint_candidate(item: dict) -> bool:
    probability = _safe_float(item.get("probability"))
    route_score = _safe_float(item.get("route_score"))
    support_strength = _safe_float(item.get("support_strength_score"))
    change_pct = _safe_float(item.get("change_pct"))
    volume_ratio = _safe_float(item.get("volume_ratio"))
    factors = item.get("probability_factors") or {}
    probe_score = _safe_float(factors.get("pre_board_probe_score"))
    strict_confirmation_count = _safe_int(factors.get("strict_confirmation_count"))
    return (
        (probability >= 0.16 or route_score >= 48.0 or probe_score >= 62.0)
        and probe_score >= PRE_BOARD_PROBE_MIN_SCORE
        and support_strength >= 42.0
        and -1.5 <= change_pct <= 8.6
        and 0.8 <= volume_ratio <= 6.8
        and strict_confirmation_count >= 1
    )


def _is_oversold_reversal_sprint_candidate(item: dict) -> bool:
    probability = _safe_float(item.get("probability"))
    route_score = _safe_float(item.get("route_score"))
    support_strength = _safe_float(item.get("support_strength_score"))
    change_pct = _safe_float(item.get("change_pct"))
    volume_ratio = _safe_float(item.get("volume_ratio"))
    sector_strength = _safe_float(item.get("sector_strength_score"))
    sector_limit_up_count = _safe_int(item.get("sector_limit_up_count"))
    factors = item.get("probability_factors") or {}
    market_risk_level = str(factors.get("market_risk_level") or "")
    oversold_score = _safe_float(factors.get("oversold_reversal_score"))
    broad_cluster = bool(factors.get("broad_rotation_cluster_setup")) or (
        bool(factors.get("market_broad_first_board_overflow"))
        and sector_limit_up_count >= BROAD_ROTATION_MAINLINE_MIN_LIMIT_UP_COUNT
    )
    return (
        market_risk_level in {"weak", "hostile"}
        and bool(factors.get("market_broad_first_board_overflow"))
        and (probability >= 0.035 or route_score >= 42.0 or oversold_score >= OVERSOLD_REVERSAL_MIN_SCORE + 8.0)
        and oversold_score >= OVERSOLD_REVERSAL_MIN_SCORE
        and support_strength >= 42.0
        and -10.5 <= change_pct <= -1.0
        and 0.55 <= volume_ratio <= 8.5
        and (broad_cluster or sector_strength >= 32.0)
    )


def _is_first_board_weak_watch_candidate(item: dict) -> bool:
    if str(item.get("candidate_route") or "") == "oversold_reversal_start":
        return not _is_oversold_reversal_sprint_candidate(item)
    return _safe_float(item.get("change_pct")) < 0.0


def _is_quiet_setup_pre_sprint_candidate(item: dict) -> bool:
    if str(item.get("candidate_route") or "") != "quiet_setup":
        return False

    probability = _safe_float(item.get("probability"))
    route_score = _safe_float(item.get("route_score"))
    sub_probabilities = item.get("sub_probabilities") or {}
    breakout_probability = _safe_float(sub_probabilities.get("breakout_3d"))
    support_strength = _safe_float(item.get("support_strength_score"))
    sector_strength = _safe_float(item.get("sector_strength_score"))
    change_pct = _safe_float(item.get("change_pct"))
    volume_ratio = _safe_float(item.get("volume_ratio"))
    factors = item.get("probability_factors") or {}
    strict_confirmation_count = _safe_int(factors.get("strict_confirmation_count"))
    qualified_doji = bool(factors.get("qualified_doji_confirmation"))
    overhead_gap_pct = _safe_float(factors.get("overhead_gap_pct"))

    return (
        (probability >= 0.46 or breakout_probability >= 0.3 or route_score >= 50.0)
        and support_strength >= 48.0
        and sector_strength >= 18.0
        and 0.8 <= change_pct < 1.2
        and 0.75 <= volume_ratio <= 1.5
        and strict_confirmation_count >= 3
        and qualified_doji
        and overhead_gap_pct <= 2.6
    )


def _is_first_board_sprint_candidate(item: dict) -> bool:
    candidate_route = str(item.get("candidate_route") or "")
    if candidate_route == "support_squeeze_start":
        return _is_support_squeeze_sprint_candidate(item)
    if candidate_route == "platform_relaunch":
        return _is_platform_relaunch_sprint_candidate(item)
    if candidate_route == "hot_primary":
        return _is_hot_primary_sprint_candidate(item)
    if candidate_route == "news_catalyst_start":
        return _is_news_catalyst_sprint_candidate(item)
    if candidate_route == "auction_surge_start":
        return _is_auction_surge_sprint_candidate(item)
    if candidate_route == "mainline_spread_start":
        return _is_mainline_spread_sprint_candidate(item)
    if candidate_route == "pre_board_probe_start":
        return _is_pre_board_probe_sprint_candidate(item)
    if candidate_route == "oversold_reversal_start":
        return _is_oversold_reversal_sprint_candidate(item)
    if candidate_route in {"fresh_hot_start", "fresh_mainline_start", "fresh_relay_start"}:
        return _is_fresh_hot_sprint_candidate(item)

    prediction_mode = str(item.get("prediction_mode") or "")
    if prediction_mode != "strict_stealth_scan":
        return False

    probability = _safe_float(item.get("probability"))
    route_score = _safe_float(item.get("route_score"))
    sub_probabilities = item.get("sub_probabilities") or {}
    breakout_probability = _safe_float(sub_probabilities.get("breakout_3d"))
    support_strength = _safe_float(item.get("support_strength_score"))
    sector_strength = _safe_float(item.get("sector_strength_score"))
    change_pct = _safe_float(item.get("change_pct"))
    volume_ratio = _safe_float(item.get("volume_ratio"))
    factors = item.get("probability_factors") or {}
    strict_confirmation_count = _safe_int(factors.get("strict_confirmation_count"))
    qualified_doji = bool(factors.get("qualified_doji_confirmation"))
    overhead_gap_pct = _safe_float(factors.get("overhead_gap_pct"))

    return (
        (probability >= 0.72 or breakout_probability >= 0.45 or route_score >= 55.0)
        and support_strength >= 55.0
        and sector_strength >= 20.0
        and 1.2 <= change_pct <= 3.8
        and 0.7 <= volume_ratio <= 1.8
        and strict_confirmation_count >= 4
        and qualified_doji
        and overhead_gap_pct <= 2.1
    )


def _annotate_first_board_timing(item: dict) -> dict:
    explicit_horizon = str(item.get("time_horizon") or "").strip()
    candidate_route = str(item.get("candidate_route") or "").strip()
    is_support_squeeze_route = candidate_route == "support_squeeze_start"

    def _support_squeeze_stage_payload(target_horizon: str) -> dict:
        is_sprint = target_horizon == "sprint"
        is_weak_watch = target_horizon == "weak_watch"
        guidance = _build_support_squeeze_last_mile_guidance(
            item,
            stage=(
                "support_squeeze_sprint"
                if is_sprint
                else "support_squeeze_weak_watch" if is_weak_watch else "support_squeeze_watch"
            ),
        )
        if is_sprint:
            return {
                "support_squeeze_stage": "support_squeeze_sprint",
                "support_squeeze_stage_label": SUPPORT_SQUEEZE_SPRINT_LABEL,
                "support_squeeze_stage_reason": "支撑位已经稳住，不管是缩量沉淀还是首次放量启动，都更接近 1-2 个交易日内的首波点火窗口。",
                "candidate_route_label": SUPPORT_SQUEEZE_SPRINT_LABEL,
                "watch_bucket": "",
                "watch_bucket_label": "",
                "watch_bucket_reason": "",
                "time_horizon": "sprint",
                "time_horizon_label": SUPPORT_SQUEEZE_SPRINT_LABEL,
                "time_horizon_reason": "支撑位启动条件已经比较齐，接下来更看首次放量点火能否直接切进首板冲刺。",
                **guidance,
            }
        if is_weak_watch:
            return {
                "support_squeeze_stage": "support_squeeze_weak_watch",
                "support_squeeze_stage_label": FIRST_BOARD_WEAK_WATCH_LABEL,
                "support_squeeze_stage_reason": "支撑区逻辑还在，但当天收绿说明弱转强没有走出来，先移到弱观察层。",
                "candidate_route_label": SUPPORT_SQUEEZE_WATCH_LABEL,
                "watch_bucket": "support_squeeze_watch",
                "watch_bucket_label": SUPPORT_SQUEEZE_WATCH_LABEL,
                "watch_bucket_reason": FIRST_BOARD_WATCH_BUCKET_DESCRIPTIONS["support_squeeze_watch"],
                "time_horizon": "weak_watch",
                "time_horizon_label": FIRST_BOARD_WEAK_WATCH_LABEL,
                "time_horizon_reason": "支撑位逻辑还在，但当天收绿，先移到弱观察层，等止跌翻红并补确认K后再回观察预备。",
                **guidance,
            }
        return {
            "support_squeeze_stage": "support_squeeze_watch",
            "support_squeeze_stage_label": SUPPORT_SQUEEZE_WATCH_LABEL,
            "support_squeeze_stage_reason": "支撑区已经稳住，但承接、涨幅或确认K还差一口气，先留在观察预备层跟踪。",
            "candidate_route_label": SUPPORT_SQUEEZE_WATCH_LABEL,
            "watch_bucket": "support_squeeze_watch",
            "watch_bucket_label": SUPPORT_SQUEEZE_WATCH_LABEL,
            "watch_bucket_reason": FIRST_BOARD_WATCH_BUCKET_DESCRIPTIONS["support_squeeze_watch"],
            "time_horizon": "watch",
            "time_horizon_label": SUPPORT_SQUEEZE_WATCH_LABEL,
            "time_horizon_reason": "支撑位已经稳住，但不管是缩量蓄势还是放量启动都还差最后一口气，先放在支撑位观察预备池继续跟踪。",
            **guidance,
        }

    if explicit_horizon in {"sprint", "pre_sprint", "watch", "weak_watch"}:
        resolved_horizon = explicit_horizon
        if explicit_horizon == "watch" and _is_first_board_weak_watch_candidate(item):
            resolved_horizon = "weak_watch"
        elif explicit_horizon == "watch" and _is_quiet_setup_pre_sprint_candidate(item):
            resolved_horizon = "pre_sprint"
        annotated = {
            **item,
            "time_horizon": resolved_horizon,
            "time_horizon_label": str(
                item.get("time_horizon_label")
                or (
                    "1-2日冲刺"
                    if resolved_horizon == "sprint"
                    else FIRST_BOARD_PRE_SPRINT_LABEL if resolved_horizon == "pre_sprint"
                    else FIRST_BOARD_WEAK_WATCH_LABEL if resolved_horizon == "weak_watch"
                    else "首板梯队"
                )
            ),
            "time_horizon_reason": str(
                item.get("time_horizon_reason")
                or (
                    "形态、承接与位置更接近 1-2 个交易日内点火"
                    if resolved_horizon == "sprint"
                    else "静默结构已经靠近冲刺窗口，但涨幅还在 0.8%-1.2% 之间，先放在准冲刺层等进一步放大确认"
                    if resolved_horizon == "pre_sprint"
                    else "当天收绿，先移到弱观察层，等重新翻红并补确认K后再回首板梯队"
                    if resolved_horizon == "weak_watch"
                    else "未到 1-2 日冲刺，但仍保留在 3-5 日首板预测梯队中继续跟踪"
                )
            ),
        }
        if is_support_squeeze_route:
            annotated.update(_support_squeeze_stage_payload(resolved_horizon))
        return annotated

    is_sprint = _is_first_board_sprint_candidate(item)
    is_pre_sprint = not is_sprint and _is_quiet_setup_pre_sprint_candidate(item)
    is_weak_watch = not is_sprint and _is_first_board_weak_watch_candidate(item)
    annotated = {
        **item,
        "time_horizon": "sprint" if is_sprint else "pre_sprint" if is_pre_sprint else "weak_watch" if is_weak_watch else "watch",
        "time_horizon_label": "1-2日冲刺" if is_sprint else FIRST_BOARD_PRE_SPRINT_LABEL if is_pre_sprint else FIRST_BOARD_WEAK_WATCH_LABEL if is_weak_watch else "首板梯队",
        "time_horizon_reason": (
            "形态、承接与位置更接近 1-2 个交易日内点火"
            if is_sprint
            else "静默结构已经靠近冲刺窗口，但涨幅还在 0.8%-1.2% 之间，先放在准冲刺层等进一步放大确认"
            if is_pre_sprint
            else "当天收绿，先移到弱观察层，等重新翻红并补确认K后再回首板梯队"
            if is_weak_watch
            else "结构完成度较好，但还没到 1-2 日冲刺窗口，先保留在首板预测梯队"
        ),
    }
    if is_support_squeeze_route:
        annotated.update(_support_squeeze_stage_payload("sprint" if is_sprint else "weak_watch" if is_weak_watch else "watch"))
    return annotated


def _first_board_display_priority(item: dict) -> tuple:
    sub_probabilities = item.get("sub_probabilities") or {}
    news_context = {
        **(item.get("probability_factors") or {}),
        **(item.get("detail") or {}),
        **item,
    }
    return (
        _first_board_tradeable_rank(item),
        1 if _is_fresh_direct_hard_news_context(news_context) else 0,
        _first_board_empirical_lift_rank_score(item),
        _safe_float(item.get("probability")),
        _safe_float(sub_probabilities.get("first_limitup_5d")),
        _safe_float(item.get("route_score")),
        _safe_float(item.get("main_uptrend_score")),
        _safe_float(item.get("bull_score")),
        _safe_float(item.get("display_score")),
    )


def _first_board_candidate_pool_priority(item: dict) -> tuple:
    """全市场池截断前优先保留硬事件、可执行候选和启动种子。"""
    factors = item.get("probability_factors") or {}
    news_context = {
        **factors,
        **(item.get("detail") or {}),
        **item,
    }
    return (
        1 if _is_fresh_direct_hard_news_context(news_context) else 0,
        1 if _is_actionable_first_board_prediction(item) else 0,
        1 if bool(item.get("prediction_shape_seed") or factors.get("prediction_shape_seed")) else 0,
        _safe_float(item.get("probability")),
        SETUP_GRADE_RANK.get(str(item.get("setup_grade") or ""), -1),
        _safe_float(item.get("bull_score")),
        _safe_float(item.get("display_score")),
    )


def _dedupe_first_board_candidates(candidates: list[dict]) -> list[dict]:
    best_by_key: dict[tuple[str, int], dict] = {}
    horizon_rank = {"sprint": 4, "pre_sprint": 3, "watch": 2, "weak_watch": 1}

    def dedupe_rank(item: dict) -> tuple:
        annotated = _annotate_first_board_timing(item)
        factors = annotated.get("probability_factors") or {}
        news_context = {
            **factors,
            **(annotated.get("detail") or {}),
            **annotated,
        }
        return (
            1 if _is_fresh_direct_hard_news_context(news_context) else 0,
            1 if not bool(annotated.get("keep_in_diagnostics")) and annotated.get("trade_ready") is not False else 0,
            1 if _is_actionable_first_board_prediction(annotated) else 0,
            horizon_rank.get(str(annotated.get("time_horizon") or ""), 0),
            _first_board_tradeable_rank(annotated),
            _first_board_empirical_lift_rank_score(annotated),
            _first_board_inverse_early_attack_score(annotated),
            _safe_float(annotated.get("probability")),
            _safe_float(annotated.get("route_score")),
            _safe_float(factors.get("news_catalyst_score")),
            _safe_float(factors.get("auction_strength_score")),
            _safe_float(factors.get("pre_board_probe_score")),
            SETUP_GRADE_RANK.get(str(annotated.get("setup_grade") or ""), -1),
            _safe_float(annotated.get("main_uptrend_score")),
            _safe_float(annotated.get("bull_score")),
            _safe_float(annotated.get("display_score")),
        )

    for item in candidates:
        code = str(item.get("code") or "").strip()
        if not code:
            continue
        key = (code, _safe_int(item.get("target_board"), 1))
        annotated = _annotate_first_board_timing(item)
        existing = best_by_key.get(key)
        if existing is None or dedupe_rank(annotated) > dedupe_rank(existing):
            best_by_key[key] = annotated
    return list(best_by_key.values())


def _split_first_board_candidates(
    candidates: list[dict],
    *,
    min_display_count: int = FIRST_BOARD_DISPLAY_MIN_COUNT,
) -> tuple[list[dict], list[dict], list[dict], list[dict]]:
    candidates = _dedupe_first_board_candidates(candidates)
    sprint: list[dict] = []
    pre_sprint: list[dict] = []
    watch: list[dict] = []
    weak_watch: list[dict] = []
    for item in candidates:
        annotated = _annotate_first_board_timing(item)
        if annotated.get("time_horizon") == "sprint":
            sprint.append(annotated)
        elif annotated.get("time_horizon") == "pre_sprint":
            pre_sprint.append(annotated)
        elif annotated.get("time_horizon") == "weak_watch":
            weak_watch.append(annotated)
        else:
            watch.append(annotated)

    sprint.sort(key=_first_board_display_priority, reverse=True)
    pre_sprint.sort(key=_first_board_display_priority, reverse=True)
    watch.sort(key=_first_board_display_priority, reverse=True)
    weak_watch.sort(key=_first_board_display_priority, reverse=True)
    actionable_sprint = [item for item in sprint if _is_actionable_first_board_prediction(item)]
    non_actionable_sprint = [item for item in sprint if item not in actionable_sprint]
    if non_actionable_sprint:
        watch = non_actionable_sprint + watch
        sprint = actionable_sprint
    has_actionable_backfill = any(_is_actionable_first_board_prediction(item) for item in pre_sprint + watch)

    if not sprint and not has_actionable_backfill:
        displayable_pre_sprint = [item for item in pre_sprint if _is_displayable_first_board_prediction(item)]
        displayable_watch = [item for item in watch if _is_displayable_first_board_prediction(item)]
        display_backfill = [
            {
                **item,
                "display_bucket": "pre_sprint_backfill",
                "display_bucket_label": FIRST_BOARD_PRE_SPRINT_LABEL,
                "display_only": True,
                "display_reason": "未达到交易可执行阈值，按准冲刺观察补位展示",
            }
            for item in displayable_pre_sprint[:min_display_count]
        ]
        remaining_needed = max(min_display_count - len(display_backfill), 0)
        display_backfill.extend(
            {
                **item,
                "display_bucket": "watch_backfill",
                "display_bucket_label": "梯队补位",
                "display_only": True,
                "display_reason": "未达到交易可执行阈值，按首板梯队观察补位展示",
            }
            for item in displayable_watch[:remaining_needed]
        )
        if display_backfill:
            display_codes = {str(item.get("code") or "") for item in display_backfill}
            return (
                display_backfill,
                [item for item in pre_sprint if str(item.get("code") or "") not in display_codes],
                [item for item in watch if str(item.get("code") or "") not in display_codes],
                weak_watch,
            )

    if len(sprint) >= min_display_count or (not pre_sprint and not watch) or len(sprint) + len(pre_sprint) + len(watch) < min_display_count:
        return sprint, pre_sprint, watch, weak_watch
    if not sprint and not has_actionable_backfill:
        return sprint, pre_sprint, watch, weak_watch

    remaining_needed = max(min_display_count - len(sprint), 0)
    actionable_pre_sprint = [item for item in pre_sprint if _is_actionable_first_board_prediction(item)]
    actionable_watch = [item for item in watch if _is_actionable_first_board_prediction(item)]
    pre_sprint_supplement_count = min(len(actionable_pre_sprint), remaining_needed)
    remaining_needed -= pre_sprint_supplement_count
    watch_supplement_count = min(len(actionable_watch), remaining_needed)

    supplemented_sprint = [
        {
            **item,
            "display_bucket": "pre_sprint_backfill",
            "display_bucket_label": FIRST_BOARD_PRE_SPRINT_LABEL,
        }
        for item in actionable_pre_sprint[:pre_sprint_supplement_count]
    ] + [
        {
            **item,
            "display_bucket": "watch_backfill",
            "display_bucket_label": "梯队补位",
        }
        for item in actionable_watch[:watch_supplement_count]
    ]
    supplemented_codes = {str(item.get("code") or "") for item in supplemented_sprint}
    return (
        sprint + supplemented_sprint,
        [item for item in pre_sprint if str(item.get("code") or "") not in supplemented_codes],
        [item for item in watch if str(item.get("code") or "") not in supplemented_codes],
        weak_watch,
    )


def _first_board_rank_key(item: dict) -> tuple:
    sub_probabilities = item.get("sub_probabilities") or {}
    factors = item.get("probability_factors") or {}
    common_tail = (
        _first_board_inverse_early_attack_score(item),
        _safe_float(item.get("learning_recall_rank_boost"), _safe_float(factors.get("learning_recall_rank_boost"))),
        _safe_float(sub_probabilities.get("first_limitup_5d")),
        _safe_float(item.get("route_score")),
        SETUP_GRADE_RANK.get(str(item.get("setup_grade") or ""), -1),
        _safe_float(item.get("bull_score")),
        _safe_float(item.get("display_score")),
    )
    if str(factors.get("prediction_runtime_mode") or "").startswith("deployed_"):
        # 人工审批后仅让挑战者概率主导“已通过召回/交易性过滤”的排序；
        # 候选召回、风险与执行闸门仍沿用独立生产规则。
        return (
            _first_board_tradeable_rank(item),
            _safe_float(item.get("probability")),
            _first_board_empirical_lift_rank_score(item),
            *common_tail,
        )
    return (
        _first_board_tradeable_rank(item),
        _first_board_empirical_lift_rank_score(item),
        _first_board_inverse_early_attack_score(item),
        _safe_float(item.get("learning_recall_rank_boost"), _safe_float(factors.get("learning_recall_rank_boost"))),
        _safe_float(item.get("probability")),
        _safe_float(sub_probabilities.get("first_limitup_5d")),
        _safe_float(item.get("route_score")),
        SETUP_GRADE_RANK.get(str(item.get("setup_grade") or ""), -1),
        _safe_float(item.get("bull_score")),
        _safe_float(item.get("display_score")),
    )


def _first_board_tradeable_rank(item: dict) -> int:
    if item.get("is_tradeable") is False:
        return 0
    if item.get("is_tradeable") is True:
        return 2
    code = str(item.get("code") or "").strip()
    if stock_tagger.is_tradeable(code):
        return 2
    board_type = stock_tagger.get_board_type(code)
    if board_type in {"gem", "star", "bse"}:
        return 1
    return 0


def _first_board_empirical_lift_rank_score(item: dict) -> float:
    """用已完成交易日的时点可见特征重排首板候选。

    2026-04-07至2026-08-28的30,240个日期匹配样本及后29日时间外
    留出显示：首板命中更依赖活跃量价、多日资金、主营行业点火、直接
    消息和近期涨停/强阳记忆；“绝对低位、静态漂亮平台、极端缩量”反而
    容易把排序推向低动能样本。该分只服务预测排序，不改写概率，也绝不
    绕过交易性、风险和买点闸门。
    """
    factors = item.get("probability_factors") or {}
    score = min(max(_safe_float(factors.get("memory_score")), 0.0), 100.0) * 0.10
    if bool(factors.get("has_recent_limit_up_event")):
        score += 4.0
    is_sector_washout_watch = bool(factors.get("sector_washout_reversal_watch"))
    auction_cluster_confirmed = bool(factors.get("auction_cluster_confirmed"))
    if bool(factors.get("prediction_shape_seed")) and not is_sector_washout_watch:
        score += min(max(_safe_float(factors.get("momentum_shakeout_score")) - 70.0, 0.0), 26.0) * 0.18
    if bool(factors.get("strong_first_bearish_ready")):
        persistence_score = min(
            max(
                _safe_float(
                    factors.get("strong_first_bearish_persistence_score")
                ),
                0.0,
            ),
            100.0,
        )
        score += 0.8 + max(persistence_score - 66.0, 0.0) * 0.08
        score += (
            2.6
            if bool(factors.get("strong_first_bearish_confirmation"))
            else -0.6
        )
    if auction_cluster_confirmed:
        cluster_count = min(max(_safe_int(factors.get("auction_sector_member_count")), 0), 12)
        cluster_avg_open = min(max(_safe_float(factors.get("auction_sector_avg_open_change")), 0.0), 6.0)
        score += 2.0 + cluster_count * 0.35 + cluster_avg_open * 0.30
        if not auction_context_complete(factors):
            score -= 0.8
    elif is_sector_washout_watch:
        # 昨收只能确认“活跃主线经历急跌”，不能确认次日一定反包。
        # 仅给很小的召回分，等 09:25 同板块竞价集群来完成二次筛选。
        score += min(max(_safe_float(factors.get("sector_washout_score")), 0.0), 100.0) * 0.006
    if bool(factors.get("board_memory_reset_ready")):
        reset_score = min(max(_safe_float(factors.get("board_memory_reset_score")), 0.0), 100.0)
        # 低位涨停记忆只负责召回。只有硬消息、竞价同题材集群或明确的
        # 板块轮动确认出现时才给足排序增益，避免把几百只相似K线都抬成
        # 次日买点。
        score += 0.4 + max(reset_score - 65.0, 0.0) * 0.035
        reset_confirmed = bool(
            auction_cluster_confirmed
            or (
                str(factors.get("news_mapping_mode") or "") == "direct_code"
                and str(factors.get("news_event_grade") or "") in {"hard", "medium"}
            )
            or _safe_float(factors.get("sector_rotation_score")) >= 58.0
            or _safe_float(factors.get("sector_limit_up_delta")) >= 2.0
        )
        score += 3.2 if reset_confirmed else -0.8
    if bool(factors.get("low_base_rotation_ready")):
        low_base_score = min(max(_safe_float(factors.get("low_base_rotation_score")), 0.0), 100.0)
        # 无近期涨停记忆的低位首波全市场同形股票很多，形态本身仅给
        # 召回分；必须由竞价同题材扩散、直接硬消息或板块宽度确认升格。
        score += 0.2 + max(low_base_score - 52.0, 0.0) * 0.025
        low_base_confirmed = bool(
            auction_cluster_confirmed
            or _is_fresh_direct_hard_news_context({**factors, **item})
            or _safe_float(factors.get("sector_limit_up_delta")) >= 2.0
            or (
                _safe_float(factors.get("sector_rotation_score")) >= 62.0
                and _safe_float(factors.get("sector_breadth_percentile")) >= 70.0
            )
        )
        score += 3.0 if low_base_confirmed else -1.0
    if bool(factors.get("low_base_sector_ignition_ready")):
        # 新研究组合必须优先于“单纯低位K线”：主营行业点火与个股修复
        # 同时成立才给稳定排序分；三日资金/直接消息确认后再小幅升档。
        ignition_score = min(
            max(_safe_float(factors.get("low_base_sector_ignition_score")), 0.0),
            100.0,
        )
        score += 1.6 + max(ignition_score - 58.0, 0.0) * 0.05
        if bool(factors.get("low_base_sector_ignition_confirmed")):
            score += 1.0
    elif bool(factors.get("funding_preheat_ready")):
        score += 0.6
    if bool(factors.get("funding_persistent_outflow_risk")):
        score -= 1.2
    score += min(max(_safe_float(factors.get("sector_rotation_score")), 0.0), 100.0) * 0.015
    score += min(max(_safe_float(factors.get("sector_strength_delta_percentile")), 0.0), 100.0) * 0.010
    score += min(max(_safe_float(factors.get("sector_fund_flow_delta_percentile")), 0.0), 100.0) * 0.012
    score += min(max(_safe_float(factors.get("sector_breadth_percentile")), 0.0), 100.0) * 0.008
    score += min(max(_safe_float(factors.get("sector_mapping_confidence_score")), 0.0), 100.0) * 0.010
    score += min(max(_safe_float(factors.get("sector_flow_persistence_score")), 0.0), 8.0) * 0.12
    news_mapping_mode = str(factors.get("news_mapping_mode") or "")
    news_event_grade = str(factors.get("news_event_grade") or "")
    if news_mapping_mode == "direct_code":
        score += 1.8 if news_event_grade == "hard" else 1.1 if news_event_grade == "medium" else 0.5
        score += 0.9 if bool(factors.get("news_direct_high_impact")) else 0.0
        score += 0.6 if bool(factors.get("news_repeated_direct")) else 0.0
    elif news_mapping_mode == "sector_inferred":
        score -= 1.2
    score += min(max(_safe_float(factors.get("upper_gap_pct")), 0.0), 8.0) * 0.15
    score += min(max(_safe_float(factors.get("lower_gap_pct")), 0.0), 8.0) * 0.12
    score += min(max(_safe_float(factors.get("burst_rebound_pct")), 0.0), 20.0) * 0.08
    score += min(max(_safe_float(factors.get("sector_limit_up_delta")), -5.0), 12.0) * 0.12
    score -= min(max(_safe_float(factors.get("platform_score")), 0.0), 100.0) * 0.05
    score -= min(max(_safe_float(factors.get("support_squeeze_signal_score")), 0.0), 100.0) * 0.035
    if bool(factors.get("has_doji_confirmation")):
        score -= 1.2
    if bool(factors.get("qualified_doji_confirmation")):
        score -= 0.8
    if bool(factors.get("has_volume_suffocation")):
        score -= 0.6
    anchor_gap = min(max(_safe_float(factors.get("limit_up_anchor_gap_pct")), 0.0), 500.0)
    score -= log1p(anchor_gap) * 0.35
    route = str(item.get("candidate_route") or "")
    if route == "mainline_spread_start":
        score += 0.4
    elif route == "oversold_reversal_start":
        score += 0.3
    elif route in {"news_catalyst_start", "auction_surge_start", "pre_board_probe_start"}:
        score += 0.2
    return round(score, 3)


def _first_board_inverse_early_attack_score(item: dict) -> float:
    factors = item.get("probability_factors") or {}
    route = str(item.get("candidate_route") or "")
    if route == "oversold_reversal_start":
        return 0.0
    if _first_board_tradeable_rank(item) < 2:
        return 0.0
    if route not in {
        "news_catalyst_start",
        "auction_surge_start",
        "mainline_spread_start",
        "pre_board_probe_start",
        "fresh_mainline_start",
        "fresh_relay_start",
        "fresh_hot_start",
    }:
        return 0.0

    market_risk_level = str(factors.get("market_risk_level") or "")
    weak_or_diffusion = (
        market_risk_level in {"weak", "hostile"}
        or bool(factors.get("market_broad_first_board_overflow"))
        or _safe_float(factors.get("market_broken_ratio")) >= 0.42
    )
    if not weak_or_diffusion:
        return 0.0

    probability = _safe_float(item.get("probability"))
    route_score = _safe_float(item.get("route_score"))
    change_pct = _safe_float(item.get("change_pct"))
    volume_ratio = _safe_float(item.get("volume_ratio"))
    support_strength = _safe_float(item.get("support_strength_score"))
    sector_strength = _safe_float(item.get("sector_strength_score"))
    main_inflow_pct = _safe_float(item.get("main_net_inflow_pct"))
    strict_confirmation_count = _safe_int(factors.get("strict_confirmation_count"))
    news_score = _safe_float(factors.get("news_catalyst_score"))
    auction_score = _safe_float(factors.get("auction_strength_score"))
    pre_board_score = _safe_float(factors.get("pre_board_probe_score"))

    has_route_edge = (
        news_score >= NEWS_CATALYST_MIN_SCORE
        or auction_score >= AUCTION_SURGE_MIN_SCORE
        or pre_board_score >= PRE_BOARD_PROBE_MIN_SCORE
        or route in {"mainline_spread_start", "fresh_mainline_start", "fresh_relay_start", "fresh_hot_start"}
    )
    if not has_route_edge:
        return 0.0
    if not (-2.5 <= change_pct <= 6.8):
        return 0.0
    if volume_ratio > 0 and not (0.45 <= volume_ratio <= 5.8):
        return 0.0
    if probability < 0.22 and route_score < 58.0:
        return 0.0
    if strict_confirmation_count < 1 and route not in {"news_catalyst_start", "auction_surge_start"}:
        return 0.0
    if support_strength < 40.0 and sector_strength < 40.0 and main_inflow_pct < 0.8:
        return 0.0

    score = (
        min(max(probability, 0.0), 0.85) * 42.0
        + min(max(route_score, 0.0), 100.0) * 0.26
        + min(max(support_strength, 0.0), 100.0) * 0.10
        + min(max(sector_strength, 0.0), 100.0) * 0.08
        + min(max(main_inflow_pct, -5.0) + 5.0, 18.0) * 0.8
        + min(max(strict_confirmation_count, 0), 5) * 1.4
    )
    if route == "news_catalyst_start":
        score += 4.0
    elif route == "auction_surge_start":
        score += 5.0
    elif route == "mainline_spread_start":
        score += 3.0
    return round(score, 3)


def _is_inverse_early_attack_first_board(item: dict) -> bool:
    return _first_board_inverse_early_attack_score(item) > 0


def _first_board_rank_lane(item: dict) -> str:
    route = str(item.get("candidate_route") or "")
    event_types = {str(value or "") for value in (item.get("event_types") or []) if str(value or "")}
    strategy_lane = str(item.get("strategy_lane") or "")
    if route == "auction_surge_start" or "auction_surge" in event_types or strategy_lane == "auction_surge_first_board":
        return "auction_surge"
    if route == "news_catalyst_start" or "news_catalyst" in event_types or strategy_lane == "news_catalyst_first_board":
        return "news_catalyst"
    factors = item.get("probability_factors") or {}
    detail = item.get("detail") or {}
    if bool(
        factors.get("strong_first_bearish_ready")
        or detail.get("strong_first_bearish_ready")
    ) and _safe_float(
        factors.get(
            "strong_first_bearish_persistence_score",
            (detail.get("strong_first_bearish_stats") or {}).get(
                "pullback_technical_persistence_score"
            ),
        )
    ) >= 66.0:
        return "trend_reclaim"
    if (
        route in {"mainline_spread_start", "fresh_mainline_start", "fresh_relay_start", "fresh_hot_start", "mainline_relay", "relay_fillup", "hot_primary"}
        or {"mainline_spread", "mainline_relay"} & event_types
        or strategy_lane in {"mainline_spread_first_board", "trend_relay_watch"}
    ):
        return "mainline_spread"
    if _is_inverse_early_attack_first_board(item):
        return "inverse_early_attack"
    return "low_absorb_halfway"


def _first_board_lane_quotas(
    limit: int,
    *,
    broad_rotation_market: bool = False,
    washout_reversal_market: bool = False,
) -> dict[str, int]:
    if limit <= 0:
        return {lane: 0 for lane in FIRST_BOARD_RANK_LANE_ORDER}
    if limit <= 3:
        return {
            lane: 1 if index < limit else 0
            for index, lane in enumerate(FIRST_BOARD_RANK_LANE_ORDER)
        }
    if washout_reversal_market:
        # 前一日普跌/大额流出后的修复日，增加“低吸/半路”形态预测配额；
        # 这些仍是 prediction_watch_only，不改变实际下单与推送闸门。
        quotas = {
            "inverse_early_attack": max(1, round(limit * 0.16)),
            "news_catalyst": max(1, round(limit * 0.14)),
            "trend_reclaim": max(1, round(limit * 0.10)),
            "auction_surge": max(1, round(limit * 0.08)),
            "mainline_spread": max(1, round(limit * 0.22)),
        }
    elif broad_rotation_market:
        quotas = {
            "inverse_early_attack": max(1, round(limit * 0.10)),
            "news_catalyst": max(1, round(limit * 0.14)),
            "trend_reclaim": max(1, round(limit * 0.14)),
            "auction_surge": max(1, round(limit * 0.10)),
            "mainline_spread": max(1, round(limit * 0.46)),
        }
    else:
        quotas = {
            "inverse_early_attack": max(1, round(limit * 0.44)),
            "news_catalyst": max(1, round(limit * 0.12)),
            "trend_reclaim": max(1, round(limit * 0.12)),
            "auction_surge": max(1, round(limit * 0.10)),
            "mainline_spread": max(1, round(limit * 0.16)),
        }
    quotas["low_absorb_halfway"] = max(1, limit - sum(quotas.values()))
    while sum(quotas.values()) > limit:
        changed = False
        minimum_per_lane = 1 if limit >= len(FIRST_BOARD_RANK_LANE_ORDER) else 0
        for lane in reversed(FIRST_BOARD_RANK_LANE_ORDER):
            lane_minimum = (
                1
                if lane == "low_absorb_halfway"
                else minimum_per_lane
            )
            if (
                quotas.get(lane, 0) > lane_minimum
                and sum(quotas.values()) > limit
            ):
                quotas[lane] -= 1
                changed = True
        if not changed:
            break
    return {lane: quotas.get(lane, 0) for lane in FIRST_BOARD_RANK_LANE_ORDER}


def _is_broad_rotation_rank_market(candidates: list[dict]) -> bool:
    mainline_broad_count = 0
    for item in candidates:
        if _first_board_rank_lane(item) != "mainline_spread":
            continue
        factors = item.get("probability_factors") or {}
        if bool(factors.get("market_broad_first_board_overflow")) or bool(factors.get("sector_catalyst_spread")):
            mainline_broad_count += 1
        if mainline_broad_count >= 3:
            return True
    return False


def _is_washout_reversal_rank_market(candidates: list[dict]) -> bool:
    washout_count = 0
    for item in candidates:
        factors = item.get("probability_factors") or {}
        detail = item.get("detail") or {}
        if not bool(
            factors.get("market_washout_reversal_setup")
            or detail.get("market_washout_reversal_setup")
        ):
            continue
        if str(item.get("candidate_route") or "") != "oversold_reversal_start":
            continue
        washout_count += 1
        if washout_count >= 3:
            return True
    return False


def _pick_first_board_lane_pool(items: list[dict], *, lane: str = "") -> list[dict]:
    prediction_pool = [
        item for item in items
        if bool(item.get("prediction_rank_eligible"))
        and (
            str(item.get("time_horizon") or "") != "weak_watch"
            or bool(item.get("prediction_shape_seed"))
            or bool((item.get("probability_factors") or {}).get("prediction_shape_seed"))
        )
    ]
    if prediction_pool:
        return prediction_pool
    if lane == "inverse_early_attack":
        return [item for item in items if item.get("time_horizon") != "weak_watch"]
    sprint = [item for item in items if item.get("time_horizon") == "sprint"]
    if sprint:
        return sprint
    pre_sprint = [item for item in items if item.get("time_horizon") == "pre_sprint"]
    if pre_sprint:
        return pre_sprint
    return [item for item in items if item.get("time_horizon") == "watch"]


def _first_board_rank_selection_key(item: dict) -> tuple[str, int]:
    return (
        str(item.get("code") or "").strip(),
        _safe_int(item.get("target_board"), 1),
    )


def _first_board_rank_sector_key(item: dict) -> str:
    factors = item.get("probability_factors") or {}
    sector_code = str(factors.get("sector_code") or item.get("sector_code") or "").strip()
    sector_name = str(factors.get("sector_name") or item.get("sector_name") or "").strip()
    return sector_code or sector_name


def _first_board_rank_broad_theme(item: dict) -> tuple[str, str]:
    """用显式板块文字生成宽主题，不使用公司名称做模糊映射。"""
    factors = item.get("probability_factors") or {}
    text = " ".join(
        str(value or "").lower()
        for value in (
            factors.get("sector_name"),
            item.get("sector_name"),
            factors.get("auction_sector_name"),
            item.get("driver_primary"),
        )
        if str(value or "").strip()
    )
    theme_words = (
        ("ai_hardware", "AI算力通信", ("cpo", "光通信", "光模块", "算力", "数据中心", "服务器", "pcb", "存储", "先进封装", "半导体", "芯片")),
        ("medicine", "医药生物", ("医药", "制药", "创新药", "医疗", "生物", "疫苗", "基因", "阿尔茨海默")),
        ("robot_equipment", "机器人装备", ("机器人", "减速器", "工业母机", "机床", "智能制造", "高端装备")),
        ("resource", "资源周期", ("黄金", "白银", "有色", "锂", "钨", "稀土", "煤炭", "油气", "化工")),
        ("agriculture", "农业食品", ("农业", "种业", "粮食", "猪肉", "养殖", "农产品", "食品", "乳业")),
        ("consumer", "消费零售", ("零售", "百货", "家居", "家电", "消费")),
        ("auto", "汽车产业链", ("汽车", "汽配", "无人驾驶", "车路云")),
        ("real_estate", "地产基建", ("房地产", "地产", "建筑", "基建", "装饰")),
        ("digital_media", "数字传媒", ("数字货币", "金融科技", "传媒", "游戏", "短剧", "影视")),
    )
    for key, label, words in theme_words:
        if any(word in text for word in words):
            return key, label
    return "", ""


def _first_board_rank_has_active_confirmation(item: dict) -> bool:
    factors = item.get("probability_factors") or {}
    return bool(
        _is_fresh_direct_hard_news_context({**factors, **item})
        or (
            bool(factors.get("auction_cluster_confirmed"))
            and auction_context_complete(factors)
        )
    )


def _first_board_rank_exposure_meta(item: dict) -> dict:
    theme_key, theme_label = _first_board_rank_broad_theme(item)
    return {
        "rank_sector_exposure_key": _first_board_rank_sector_key(item),
        "rank_broad_theme": theme_key,
        "rank_broad_theme_label": theme_label,
        "rank_active_confirmation": _first_board_rank_has_active_confirmation(item),
    }


def _first_board_trade_actionability(item: dict) -> dict:
    factors = item.get("probability_factors") or {}
    active_confirmation = _first_board_rank_has_active_confirmation(item)
    # 归因出参：把"哪一条把首板候选判成不可执行"记下来。
    # 实测九条首板路线 5.6 万条预测 actionable 恒 False，此前无法归因。
    reject_reasons: list[str] = []
    actionable = _is_actionable_first_board_prediction(item, reject_reasons=reject_reasons)
    strict_confirmation_count = _safe_int(factors.get("strict_confirmation_count"))
    score = 12.0
    score += 38.0 if actionable else 0.0
    score += 34.0 if active_confirmation else 0.0
    score += min(strict_confirmation_count, 4) * 4.0
    if bool(factors.get("auction_cluster_confirmed")) and not auction_context_complete(factors):
        score -= 24.0
    score = round(max(0.0, min(100.0, score)), 1)
    if actionable and active_confirmation:
        status, label = "confirmed_monitor", "盘口确认·可监控"
    elif actionable:
        status, label = "conditional", "条件观察·待确认"
    else:
        status, label = "forecast_only", "仅预测·不可交易"
    if not active_confirmation:
        # 这一级与路线门槛是**两个独立**的失败原因，必须分开记账：
        # `active_confirmation` 只有"盘后硬新闻"或"已验证竞价证据"两条来源。
        reject_reasons.append(
            "active_confirmation_false:no_fresh_hard_news_and_no_verified_auction_evidence"
        )
    executable = bool(actionable and active_confirmation)
    return {
        "trade_actionability_score": score,
        "trade_actionability_status": status,
        "trade_actionability_label": label,
        "prediction_actionable": executable,
        # 纯诊断：可执行时为空列表；不可执行时按发生顺序列出每条判负的子条件。
        "prediction_not_actionable_reasons": [] if executable else reject_reasons,
        "prediction_route_level_actionable": bool(actionable),
        "prediction_active_confirmation": bool(active_confirmation),
    }


def _first_board_rank_confirmation_allowed(item: dict, selected: list[dict]) -> bool:
    if _first_board_rank_has_active_confirmation(item):
        return True
    unconfirmed_count = sum(
        1
        for existing in selected
        if not bool(existing.get("rank_active_confirmation"))
    )
    return unconfirmed_count < PROMOTION_MAX_UNCONFIRMED_FIRST_BOARD_RANKED


def _first_board_rank_exposure_allowed(item: dict, selected: list[dict]) -> bool:
    meta = _first_board_rank_exposure_meta(item)
    sector_key = str(meta.get("rank_sector_exposure_key") or "")
    theme_key = str(meta.get("rank_broad_theme") or "")
    if not sector_key and not theme_key:
        return True
    actively_confirmed = bool(meta.get("rank_active_confirmation"))
    exact_cap = 4 if actively_confirmed else 2
    theme_cap = 6 if actively_confirmed else 3
    if sector_key:
        exact_count = sum(
            1
            for existing in selected
            if str((existing.get("rank_sector_exposure_key") or _first_board_rank_sector_key(existing))) == sector_key
        )
        if exact_count >= exact_cap:
            return False
    if theme_key:
        theme_count = sum(
            1
            for existing in selected
            if str((existing.get("rank_broad_theme") or _first_board_rank_broad_theme(existing)[0])) == theme_key
        )
        if theme_count >= theme_cap:
            return False
    return True


def _rank_first_board_by_lanes(candidates: list[dict], limit: int) -> list[dict]:
    """按全局时点可见证据排序，赛道只解释、不再做硬配额或删除。"""
    if limit <= 0:
        return []
    selected: list[dict] = []
    selected_keys: set[tuple[str, int]] = set()
    lane_counts: dict[str, int] = defaultdict(int)
    for item in sorted(candidates, key=_first_board_rank_key, reverse=True):
        key = _first_board_rank_selection_key(item)
        if not key[0] or key in selected_keys:
            continue
        lane = _first_board_rank_lane(item)
        lane_counts[lane] += 1
        selected_keys.add(key)
        selected.append(
            {
                **item,
                **_first_board_rank_exposure_meta(item),
                **_first_board_trade_actionability(item),
                "historical_lift_rank_score": _first_board_empirical_lift_rank_score(item),
                "historical_ignition_rank_score": _first_board_empirical_lift_rank_score(item),
                "rank_policy": "global_time_visible_evidence",
                "rank_lane": lane,
                "rank_lane_label": FIRST_BOARD_RANK_LANE_LABELS.get(lane, lane),
                "rank_lane_quota": None,
                "rank_lane_position": lane_counts[lane],
            }
        )
        if len(selected) >= limit:
            break
    # 板块集中、赛道集中和未确认数量只通过元数据暴露给执行风控；历史回放
    # 已证明在预测阶段硬删这些样本会损伤真实首板召回。
    return selected


def _rank_first_board_candidates(candidates: list[dict], limit: int) -> list[dict]:
    annotated_candidates = [_annotate_first_board_timing(item) for item in candidates]
    ranked: list[dict] = []
    supported_routes = {
        "support_squeeze_start",
        "hot_primary",
        "news_catalyst_start",
        "auction_surge_start",
        "mainline_spread_start",
        "pre_board_probe_start",
        "oversold_reversal_start",
        "fresh_hot_start",
        "fresh_mainline_start",
        "fresh_relay_start",
        "mainline_relay",
        "relay_fillup",
        "quiet_setup",
    }
    for item in annotated_candidates:
        if _first_board_tradeable_rank(item) < 2:
            continue
        if str(item.get("time_horizon") or "") == "weak_watch":
            continue
        factors = item.get("probability_factors") or {}
        actionable = _is_actionable_first_board_prediction(item)
        shape_seed = bool(
            item.get("prediction_shape_seed")
            or factors.get("prediction_shape_seed")
        )
        route = str(item.get("candidate_route") or "")
        prediction_mode = str(item.get("prediction_mode") or "")
        evidence_supported = bool(
            actionable
            or shape_seed
            or item.get("prediction_rank_eligible")
            or route in supported_routes
            or prediction_mode in {"strict", "strict_stealth_scan"}
        )
        # 预测概率与交易执行性彻底拆开：证据路线可以参与排序，但没有竞价、
        # 盘口和风险确认时仍保持 forecast_only，绝不伪装成买点。
        if not evidence_supported:
            continue
        ranked.append(
            {
                **item,
                "prediction_rank_eligible": True,
                "prediction_watch_only": not actionable,
                "prediction_actionability_label": (
                    "冲刺候选" if actionable else "概率预测·待执行确认"
                ),
                "rank_fallback": not actionable,
                "rank_fallback_reason": (
                    "时点可见证据进入预测排序；交易仍等待竞价、盘口与风险闸门"
                    if not actionable
                    else ""
                ),
                "display_only": bool(item.get("display_only")) or not actionable,
                "trade_ready": bool(item.get("trade_ready", True)) and actionable,
                "setup_grade": (
                    item.get("setup_grade") if actionable else "B类观察候选"
                ),
                "setup_grade_display": (
                    item.get("setup_grade_display") if actionable else "概率预测"
                ),
            }
        )
    return _rank_first_board_by_lanes(ranked, limit)


def _rank_first_board_recall_candidates(
    candidates: list[dict],
    formal_candidates: list[dict],
    limit: int,
) -> list[dict]:
    """构造 Top30 宽召回榜，并保证正式 Top12 是稳定前缀。"""
    recall_limit = max(0, min(limit, PROMOTION_MAX_RECALL_RANKED))
    if recall_limit <= 0:
        return []
    formal_prefix = formal_candidates[:recall_limit]
    selected_keys = {
        _first_board_rank_selection_key(item)
        for item in formal_prefix
    }
    eligible = _rank_first_board_candidates(
        candidates,
        max(len(candidates), recall_limit),
    )
    additions = [
        item
        for item in eligible
        if _first_board_rank_selection_key(item) not in selected_keys
    ]
    additions.sort(
        key=lambda item: (
            _safe_float(
                item.get("temporal_event_probability"),
                _safe_float(
                    (item.get("probability_factors") or {}).get(
                        "temporal_event_probability"
                    )
                ),
            ),
            *_first_board_rank_key(item),
        ),
        reverse=True,
    )
    combined = [
        *formal_prefix,
        *additions[: max(recall_limit - len(formal_prefix), 0)],
    ]
    return [
        {
            **item,
            "recall_rank_position": index,
            "recall_rank_tier": (
                "formal_top12" if index <= len(formal_prefix) else "broad_recall"
            ),
        }
        for index, item in enumerate(combined, start=1)
    ]


def _is_actionable_first_board_prediction(
    item: dict, *, reject_reasons: list[str] | None = None
) -> bool:
    """路线级可执行判定。

    `reject_reasons` 是**纯诊断出参**：传进来时，每个把判定判负的子条件会把
    自己的短标签追加进去。默认 None 时行为与旧版逐位相同 —— 条件表达式一个字
    未改，只是把"哪一条说不"记下来。

    为什么需要它：实测九条首板路线合计约 5.6 万条预测 `actionable` 恒为 False，
    但无法回答"到底是路线门槛挡的，还是别的东西"。而 `support_strength_score`
    / `main_net_inflow_pct` 这两个输入**没有出现在 `promotion_prediction_record`
    的 factors_json 里**（`sector_strength_score`、`route_score` 都在），
    因此"这两个字段在运行时是否真的存在"无法从落库数据反推 —— 若不存在，
    经 `_safe_float` 变 0.0 会让 `main_inflow_pct >= -6.5` 这类负阈值守护
    **被空值静默绕过**。本出参正是为了把这件事一次定死。
    """
    def _no(reason: str) -> bool:
        if reject_reasons is not None:
            reject_reasons.append(reason)
        return False

    def _clause(ok: bool, reason: str) -> bool:
        if not ok and reject_reasons is not None:
            reject_reasons.append(reason)
        return bool(ok)

    if bool(item.get("keep_in_diagnostics")) or item.get("trade_ready") is False:
        return _no("keep_in_diagnostics_or_not_trade_ready")
    if str(item.get("time_horizon") or "") == "weak_watch":
        return _no("time_horizon_weak_watch")

    probability = _safe_float(item.get("probability"))
    factors = item.get("probability_factors") or {}
    market_risk_level = str(factors.get("market_risk_level") or "")
    route = str(item.get("candidate_route") or "")
    if route == "oversold_reversal_start":
        return _clause(_is_oversold_reversal_sprint_candidate(item),
                       "route_gate:oversold_reversal_start")
    if route == "auction_surge_start" and (
        "auction_open_change" in factors
        or "auction_open_change" in (item.get("detail") or {})
    ):
        return _clause(_is_auction_surge_sprint_candidate(item),
                       "route_gate:auction_surge_start")

    # 封板基因过滤：news_catalyst_start / mainline_spread_start 是"题材/板
    # 块扩散"路径，依赖票本身具有真实封板历史。如果近 180 天内从未涨停
    # （尤其是证券/银行/中字头大盘蓝筹），即使题材契合度高，次日封板的
    # 物理概率也偏低（流通市值大、机构持仓集中、缺乏游资接力）。复盘
    # 60 天数据：news_catalyst_start 路径证券板块命中率 0.8%，非证券 1.5%；
    # 60.9% 的证券票过去 180 天从未涨停，预测池却占 15.6%。该过滤把
    # "无涨停基因"的票拦在主榜外，但仍保留在观察池供复盘。
    if route in {"news_catalyst_start", "mainline_spread_start"}:
        memory_features = item.get("memory_features") or {}
        if memory_features:
            memory_hits_50d = _safe_int(memory_features.get("memory_limit_up_hits_50d"))
            memory_hits_120d = _safe_int(memory_features.get("memory_limit_up_hits_120d"))
            days_since_last = _safe_int(memory_features.get("memory_days_since_last_limit_up"), 999)
            # 近 120 天有过涨停 OR 近 50 天有过 1 次以上涨停 = 有"封板基因"
            has_seal_gene = memory_hits_120d >= 1 or memory_hits_50d >= 1 or days_since_last <= 120
            if not has_seal_gene:
                return _no("seal_gene_missing")
        else:
            # 空 memory_features 会让上面整段被跳过（静默放行）。单独记账，
            # 便于判断这个过滤在生产里到底有没有生效。
            if reject_reasons is not None:
                reject_reasons.append("seal_gene_filter_skipped_no_memory_features")

    # probability 已是以路线真实后验命中率为中心的校准值，首板
    # 常态基准本就在2%~4%。继续沿用旧的6%~12%阈值会把所有候选清空；
    # 结构、题材和可交易闸门仍在后面独立执行。近期实测校准后概率大多
    # 落在 1%~3% 区间，再继续 0.030 阈值会把温和整理/冲板回落样本全部
    # 挡在主榜外；下调到 0.018 给正式候选池保留一定召回空间。
    min_probability = 0.015 if market_risk_level == "hostile" else 0.018 if market_risk_level == "weak" else 0.020
    if market_risk_level == "hostile" and route == "oversold_reversal_start":
        min_probability = 0.014
    elif market_risk_level == "hostile" and route == "pre_board_probe_start":
        min_probability = 0.016
    elif (
        market_risk_level == "hostile"
        and route == "mainline_spread_start"
        and (bool(factors.get("market_broad_first_board_overflow")) or bool(factors.get("sector_catalyst_spread")))
        and bool(factors.get("broad_rotation_member_setup"))
    ):
        min_probability = 0.018
    if probability < min_probability:
        return _no(f"probability_below_min:{probability:.4f}<{min_probability:.4f}")

    support_strength_known = item.get("support_strength_score") is not None
    main_inflow_known = item.get("main_net_inflow_pct") is not None
    support_strength = _safe_float(item.get("support_strength_score"))
    main_inflow_pct = _safe_float(item.get("main_net_inflow_pct"))
    strict_confirmation_count = _safe_int(factors.get("strict_confirmation_count"))
    if reject_reasons is not None and not (support_strength_known and main_inflow_known):
        # 只记账，不改变判定（本次不修行为，先取证）。
        reject_reasons.append(
            "input_missing:support_strength_score={} main_net_inflow_pct={}".format(
                "present" if support_strength_known else "ABSENT",
                "present" if main_inflow_known else "ABSENT",
            )
        )
    if market_risk_level == "hostile":
        route_score = _safe_float(item.get("route_score"))
        if route == "pre_board_probe_start":
            return _clause(
                support_strength >= 48
                and route_score >= 48
                and strict_confirmation_count >= 1
                and _safe_float(factors.get("pre_board_probe_score")) >= PRE_BOARD_PROBE_MIN_SCORE,
                "hostile:pre_board_probe_clause",
            )
        if route == "news_catalyst_start":
            return _clause(
                route_score >= 46
                and support_strength >= 38
                and _safe_float(factors.get("news_catalyst_score")) >= NEWS_CATALYST_MIN_SCORE + 8.0
                and main_inflow_pct >= -4.0,
                "hostile:news_catalyst_clause",
            )
        if route == "auction_surge_start":
            auction_feed_complete = auction_context_complete(factors)
            return _clause(
                route_score >= 44
                and support_strength >= 38
                and _safe_float(factors.get("auction_strength_score")) >= AUCTION_SURGE_MIN_SCORE + 8.0
                and _safe_float(factors.get("auction_open_change")) >= AUCTION_SURGE_MIN_OPEN_CHANGE
                and _safe_float(factors.get("auction_open_change")) < AUCTION_SURGE_MAX_OPEN_CHANGE
                and auction_feed_complete,
                "hostile:auction_surge_clause",
            )
        if route == "mainline_spread_start":
            broad_rotation_cluster = bool(factors.get("broad_rotation_cluster_setup"))
            if bool(factors.get("broad_rotation_member_setup")) and (
                bool(factors.get("market_broad_first_board_overflow"))
                or bool(factors.get("sector_catalyst_spread"))
            ):
                return _clause(
                    route_score >= (36 if broad_rotation_cluster else 38)
                    and (
                        support_strength >= (36 if broad_rotation_cluster else 40)
                        or main_inflow_pct >= -6.5
                    )
                    and (
                        broad_rotation_cluster
                        or _safe_float(item.get("sector_strength_score")) >= 55.0
                    ),
                    "hostile:mainline_relaxed_clause",
                )
            return _clause(
                route_score >= 46
                and (
                    support_strength >= 42
                    or bool(factors.get("market_broad_first_board_overflow"))
                    or main_inflow_pct >= 1.5
                )
                and (
                    broad_rotation_cluster
                    or _safe_float(item.get("sector_strength_score")) >= BROAD_ROTATION_MAINLINE_MIN_SECTOR_STRENGTH
                ),
                "hostile:mainline_strict_clause",
            )
        return _clause(
            support_strength >= 60
            and main_inflow_pct >= 5
            and strict_confirmation_count >= 3
            and route in {"fresh_mainline_start", "support_squeeze_start", "platform_relaunch"},
            "hostile:generic_route_clause",
        )
    return True


def _is_displayable_first_board_prediction(item: dict) -> bool:
    if bool(item.get("keep_in_diagnostics")) or item.get("trade_ready") is False:
        return False
    return str(item.get("time_horizon") or "") != "weak_watch"


def _second_board_candidate_factor(item: dict, key: str, default=None):
    factors = item.get("probability_factors") or {}
    value = item.get(key)
    if value is None or value == "":
        return factors.get(key, default)
    return value


def _second_board_tradeable_rank(item: dict) -> int:
    if item.get("is_tradeable") is False:
        return 0
    code = str(item.get("code") or "").strip()
    if stock_tagger.is_tradeable(code):
        return 2
    board_type = stock_tagger.get_board_type(code)
    if board_type in {"gem", "star", "bse"}:
        return 1
    return 0


def _second_board_quality_rank_score(item: dict) -> float:
    """二板主榜排序使用的封板质量分，不改变概率本身."""
    factors = item.get("probability_factors") or {}
    route = str(
        item.get("second_board_route")
        or factors.get("second_board_route")
        or "standard_second_board"
    )
    score = 0.0
    if route == "low_rotation_cluster_second_board":
        score += 5.0
    elif route in {"low_rotation_second_board", "cluster_second_board"}:
        score += 3.5
    elif route == "diffusion_second_board":
        score += 2.0

    limit_up_minutes = _safe_int(_second_board_candidate_factor(item, "limit_up_minutes"), -1)
    if limit_up_minutes < 0:
        limit_up_minutes = _parse_limit_up_minutes(str(item.get("limit_up_time") or "")) or -1
    if 0 <= limit_up_minutes <= 9 * 60 + 35:
        score += 3.0
    elif 0 <= limit_up_minutes <= 9 * 60 + 45:
        score += 2.4
    elif 0 <= limit_up_minutes <= 10 * 60:
        score += 1.6
    elif limit_up_minutes >= 14 * 60:
        score -= 2.0

    break_count = _safe_int(_second_board_candidate_factor(item, "break_count"))
    if break_count == 0:
        score += 2.0
    elif break_count == 1:
        score += 0.8
    elif break_count >= 3:
        score -= 1.5

    seal_amount = _safe_float(_second_board_candidate_factor(item, "seal_amount"))
    if seal_amount >= 2e8:
        score += 2.0
    elif seal_amount >= 1e8:
        score += 1.5
    elif seal_amount >= 5e7:
        score += 1.0

    main_inflow_pct = _safe_float(_second_board_candidate_factor(item, "main_net_inflow_pct"))
    main_inflow = _safe_float(_second_board_candidate_factor(item, "main_net_inflow"))
    if main_inflow_pct >= 6.0 or main_inflow >= 50_000_000:
        score += 1.5
    elif main_inflow_pct >= 3.0 or main_inflow >= 20_000_000:
        score += 0.8
    elif main_inflow_pct <= -5.0 or main_inflow <= -30_000_000:
        score -= 1.2

    if bool(factors.get("is_clean_early_hard_board")):
        score += 1.5
    if bool(factors.get("is_zero_break_hard_seal")):
        score += 1.0
    if bool(factors.get("has_strong_main_inflow")):
        score += 0.8
    if bool(factors.get("sector_morning_strengthening")):
        score += 0.8
    if _safe_float(factors.get("tail_seal_penalty")) > 0:
        score -= 1.2
    score += max(min(_safe_float(item.get("relay_quality_score")) - 60.0, 24.0), -20.0) * 0.18
    if item.get("relay_pool_ready") is False:
        score -= 12.0
    return round(score, 3)


def _rank_second_board_candidates(candidates: list[dict], limit: int) -> list[dict]:
    # “能否晋级”与“能否买”是两个口径。一字/准一字在强题材集群中可能
    # 有较高晋级概率，但没有正常成交机会，允许进入预测榜用于复盘，仍将
    # trade_ready 固定为 false，牛股雷达/飞书不会把它当追板买点。
    ranked: list[dict] = []
    for source_item in candidates:
        if _second_board_tradeable_rank(source_item) < 2:
            continue
        prediction_ready = source_item.get("relay_prediction_ready")
        pool_ready = source_item.get("relay_pool_ready")
        # 兼容旧快照：缺少新字段时沿用历史可交易闸门。
        if prediction_ready is False or (prediction_ready is None and pool_ready is False):
            continue
        item = dict(source_item)
        if pool_ready is False:
            item["prediction_only"] = True
            item["trade_ready"] = False
            item["relay_action_label"] = "只预测·不可追"
            item["trade_actionability_status"] = "forecast_only"
            item["trade_actionability_label"] = "只预测·不可追"
            item["trade_actionability_score"] = round(
                min(_safe_float(item.get("relay_prediction_quality_score")), 55.0),
                1,
            )
        else:
            item["prediction_only"] = False
            item["trade_ready"] = True
            item["relay_action_label"] = "可交易监控"
            item["trade_actionability_status"] = "conditional_monitor"
            item["trade_actionability_label"] = "可交易监控"
            item["trade_actionability_score"] = round(
                max(68.0, min(_safe_float(item.get("relay_quality_score")), 100.0)),
                1,
            )
        ranked.append(item)
    def rank_key(item: dict) -> tuple:
        factors = item.get("probability_factors") or {}
        tradeable = _second_board_tradeable_rank(item)
        prediction_ready = 1 if bool(
            item.get("relay_prediction_ready", item.get("relay_pool_ready", True))
        ) else 0
        quality = _safe_float(
            item.get("relay_prediction_quality_score"),
            _safe_float(item.get("relay_quality_score")),
        )
        seal_quality = _second_board_quality_rank_score(item)
        probability = _safe_float(item.get("probability"))
        tail = (
            _safe_float(item.get("second_board_style_score")),
            _safe_float(item.get("route_score")),
            _safe_float(item.get("sector_strength_score")),
            _safe_float(item.get("dragon_tiger_score")),
            -_safe_float(item.get("turnover")),
        )
        if str(factors.get("prediction_runtime_mode") or "").startswith("deployed_"):
            return (tradeable, prediction_ready, probability, quality, seal_quality, *tail)
        return (tradeable, prediction_ready, quality, seal_quality, probability, *tail)

    ranked.sort(key=rank_key, reverse=True)
    return ranked[:limit]


def _build_second_board_relay_gate(
    *,
    item: dict,
    factors: dict,
    sector: dict,
    position: dict,
) -> dict:
    """把“次日二板概率”与“能否进入可交易监控池”分开。"""
    turnover = _safe_float(item.get("turnover"))
    break_count = _safe_int(item.get("break_count"))
    limit_up_minutes = _safe_int(factors.get("limit_up_minutes"), -1)
    one_word = bool(factors.get("is_one_word_shape") or factors.get("is_one_word_like"))
    clean_hard_board = bool(
        factors.get("is_clean_early_hard_board")
        or factors.get("is_zero_break_hard_seal")
    )
    main_inflow = _safe_float(factors.get("main_net_inflow"))
    main_inflow_pct = _safe_float(factors.get("main_net_inflow_pct"))
    reason_cohort = _safe_int(factors.get("reason_cohort_count"))
    sector_cohort = _safe_int(sector.get("limit_up_count"))
    morning_strengthening = bool(factors.get("sector_morning_strengthening"))
    independent_hard_board = bool(
        clean_hard_board
        and main_inflow >= 50_000_000
        and main_inflow_pct >= 5.0
    )
    low_position = bool(position.get("preboard_low_position"))
    return_20d = _safe_float(position.get("preboard_return_20d"))
    position_60 = _safe_float(position.get("preboard_position_60"), 1.0)
    recent_boards = _safe_int(position.get("preboard_recent_board_count"))
    market_risk = str(factors.get("market_risk_level") or "")

    blockers: list[str] = []
    if one_word:
        blockers.append("首板一字/准一字，没有可验证的换手成本")
    if not (2.0 <= turnover <= 16.0):
        blockers.append(f"首板换手{turnover:.1f}%不在2%~16%可接力区间")
    if break_count > 2:
        blockers.append(f"首板炸板{break_count}次，筹码松动")
    if limit_up_minutes < 0 or limit_up_minutes > 10 * 60 + 30:
        blockers.append("封板时间过晚或缺失")
    if not low_position or return_20d > 35.0 or position_60 > 0.88:
        blockers.append(
            f"首板前20日{return_20d:+.1f}%/60日位置{position_60:.0%}，不属于低位首板"
        )
    if recent_boards > 1:
        blockers.append(f"首板前20日已有{recent_boards}次强板，不是新启动")
    has_cohort = bool(reason_cohort >= 2 or sector_cohort >= 2 or morning_strengthening)
    if not (has_cohort or independent_hard_board):
        blockers.append("无同题材梯队，且不是强资金独立硬板")

    quality_score = 48.0
    quality_score += 12.0 if clean_hard_board else 4.0
    quality_score += 8.0 if break_count == 0 else 3.0 if break_count == 1 else -3.0
    quality_score += 7.0 if 3.0 <= turnover <= 10.0 else 3.0 if 2.0 <= turnover <= 16.0 else -8.0
    quality_score += 8.0 if reason_cohort >= 3 else 5.0 if reason_cohort >= 2 else 0.0
    quality_score += 6.0 if morning_strengthening else 0.0
    quality_score += 7.0 if independent_hard_board else 0.0
    quality_score += 8.0 if low_position and position_60 <= 0.65 else 4.0 if low_position else -8.0
    quality_score += 4.0 if _safe_float(factors.get("dragon_tiger_probability_delta")) > 0 else 0.0
    quality_score -= 18.0 if one_word else 0.0
    quality_score -= 8.0 if market_risk == "hostile" and not (has_cohort and clean_hard_board) else 0.0
    quality_score = round(max(0.0, min(100.0, quality_score)), 2)
    ready = bool(not blockers and quality_score >= 68.0)
    reason_strong_cluster = bool(factors.get("reason_strong_cluster"))
    prediction_score = 44.0
    prediction_score += 16.0 if reason_cohort >= 8 else 11.0 if reason_cohort >= 3 else 5.0 if reason_cohort >= 2 else 0.0
    prediction_score += 14.0 if one_word else 7.0 if clean_hard_board else 2.0
    prediction_score += 12.0 if 0 <= limit_up_minutes <= 9 * 60 + 25 else 8.0 if 0 <= limit_up_minutes <= 9 * 60 + 35 else 3.0 if 0 <= limit_up_minutes <= 9 * 60 + 45 else -8.0
    prediction_score += 7.0 if break_count == 0 else 3.0 if break_count <= 2 else -10.0
    prediction_score += 7.0 if low_position and position_60 <= 0.65 else 3.0 if position_60 <= 0.88 else -8.0
    prediction_score += 5.0 if morning_strengthening else 0.0
    prediction_score += 4.0 if reason_strong_cluster else 0.0
    prediction_score += 4.0 if independent_hard_board else 0.0
    prediction_score -= 8.0 if return_20d > 45.0 else 0.0
    prediction_score -= 8.0 if market_risk == "hostile" and not reason_strong_cluster else 0.0
    prediction_score = round(max(0.0, min(100.0, prediction_score)), 2)
    cluster_prediction_ready = bool(
        reason_strong_cluster
        and reason_cohort >= 3
        and 0 <= limit_up_minutes <= 9 * 60 + 45
        and break_count <= 2
        and return_20d <= 45.0
        and position_60 <= 0.92
        and prediction_score >= 72.0
    )
    prediction_ready = bool(ready or cluster_prediction_ready)
    prediction_blockers: list[str] = []
    if not prediction_ready:
        if not reason_strong_cluster and not ready:
            prediction_blockers.append("无强题材首板集群")
        if limit_up_minutes < 0 or limit_up_minutes > 9 * 60 + 45:
            prediction_blockers.append("首封不在早盘强势窗口")
        if return_20d > 45.0 or position_60 > 0.92:
            prediction_blockers.append("首板前位置过高")
        if break_count > 2:
            prediction_blockers.append("首板炸板次数过多")
    route = str(factors.get("second_board_route") or "standard_second_board")
    learning_bucket = f"T2:{route}"
    return {
        "relay_pool_ready": ready,
        "relay_quality_score": quality_score,
        "relay_blockers": list(dict.fromkeys(blockers)),
        "relay_prediction_ready": prediction_ready,
        "relay_prediction_quality_score": prediction_score,
        "relay_prediction_only": bool(prediction_ready and not ready),
        "relay_prediction_blockers": list(dict.fromkeys(prediction_blockers)),
        "relay_confirmation_required": True,
        "relay_learning_bucket": learning_bucket,
    }


def _limit_up_reason_theme_keys(reason: str) -> list[tuple[str, str]]:
    """只按涨停原因中的明确产业词归并题材，不借热门板块做模糊映射。"""
    normalized = str(reason or "").strip().lower()
    if not normalized:
        return []
    theme_rules = (
        ("optical_compute", "光通信/算力", ("cpo", "光模块", "光通信", "光网络", "光纤", "硅光", "光芯片", "算力", "数据中心")),
        ("robot_equipment", "机器人/高端装备", ("机器人", "减速器", "伺服", "工业母机", "数控", "机器视觉", "智能制造")),
        ("metal_resource", "有色资源", ("黄金", "白银", "贵金属", "有色", "稀土", "钨", "锗", "锂", "铜", "镍", "钴")),
        ("semiconductor", "半导体", ("半导体", "芯片", "存储", "封测", "sic", "功率器件", "晶圆")),
        ("medicine_bio", "医药生物", ("mrna", "创新药", "医药", "制药", "疫苗", "glp", "生物医药", "医疗")),
        ("consumer_electronics", "消费电子", ("折叠屏", "消费电子", "苹果", "ai眼镜", "声学", "可穿戴")),
        ("digital_finance", "数字金融", ("数字货币", "跨境支付", "金融科技", "稳定币", "区块链")),
        ("auto_chain", "汽车产业链", ("汽车零部件", "新能源车", "无人驾驶", "智能驾驶", "车联网")),
    )
    return [
        (key, label)
        for key, label, keywords in theme_rules
        if any(keyword in normalized for keyword in keywords)
    ]


def _build_second_board_reason_context(first_boards: list[dict], spot_map: dict[str, StockSpot]) -> dict[str, dict]:
    grouped: dict[str, list[dict]] = {}
    for item in first_boards:
        reason = str(item.get("limit_up_reason") or "").strip()
        code = str(item.get("code") or "").strip()
        if not reason or not code:
            continue
        spot = spot_map.get(code)
        minutes = _parse_limit_up_minutes(item.get("limit_up_time"))
        grouped.setdefault(reason, []).append(
            {
                "code": code,
                "turnover": _safe_float(item.get("turnover")),
                "break_count": _safe_int(item.get("break_count")),
                "seal_amount": _safe_float(item.get("seal_amount")),
                "support_strength": _safe_float(getattr(spot, "support_strength_score", 0)),
                "circ_market_cap": _safe_float(getattr(spot, "circ_market_cap", 0)),
                "is_early_seal": bool(minutes is not None and minutes <= 9 * 60 + 45),
            }
        )

    context_by_code: dict[str, dict] = {}
    for reason, items in grouped.items():
        count = len(items)
        avg_turnover = sum(_safe_float(item.get("turnover")) for item in items) / count if count else 0.0
        avg_break = sum(_safe_int(item.get("break_count")) for item in items) / count if count else 0.0
        early_ratio = sum(1 for item in items if item.get("is_early_seal")) / count if count else 0.0
        avg_support = sum(_safe_float(item.get("support_strength")) for item in items) / count if count else 0.0
        avg_seal_amount = sum(_safe_float(item.get("seal_amount")) for item in items) / count if count else 0.0
        strong_cluster = bool(
            count >= 2
            and avg_break <= 1.2
            and (
                early_ratio >= 0.34
                or avg_seal_amount >= 150_000_000
                or avg_support >= 58.0
            )
        )
        for item in items:
            context_by_code[item["code"]] = {
                "reason": reason,
                "reason_cohort_count": count,
                "reason_avg_turnover": round(avg_turnover, 2),
                "reason_avg_break_count": round(avg_break, 2),
                "reason_early_seal_ratio": round(early_ratio, 3),
                "reason_avg_support_strength": round(avg_support, 2),
                "reason_avg_seal_amount": round(avg_seal_amount, 2),
                "reason_strong_cluster": strong_cluster,
            }

    # 数据源有时给行业简称，有时给“题材A+题材B+公告”的详细归因；仅按
    # 完整字符串分组会把同一条产业链误拆成独苗。这里使用原因文本中明确
    # 出现的产业词做第二层归并，并保留原始原因用于页面展示与审计。
    theme_grouped: dict[tuple[str, str], list[dict]] = {}
    for item in first_boards:
        code = str(item.get("code") or "").strip()
        reason = str(item.get("limit_up_reason") or "").strip()
        if not code or not reason:
            continue
        spot = spot_map.get(code)
        minutes = _parse_limit_up_minutes(item.get("limit_up_time"))
        normalized = {
            "code": code,
            "turnover": _safe_float(item.get("turnover")),
            "break_count": _safe_int(item.get("break_count")),
            "seal_amount": _safe_float(item.get("seal_amount")),
            "support_strength": _safe_float(getattr(spot, "support_strength_score", 0)),
            "is_early_seal": bool(minutes is not None and minutes <= 9 * 60 + 45),
        }
        for theme_key in _limit_up_reason_theme_keys(reason):
            theme_grouped.setdefault(theme_key, []).append(normalized)

    for (theme_key, theme_label), items in theme_grouped.items():
        count = len(items)
        avg_break = sum(_safe_int(item.get("break_count")) for item in items) / count if count else 0.0
        early_ratio = sum(1 for item in items if item.get("is_early_seal")) / count if count else 0.0
        avg_support = sum(_safe_float(item.get("support_strength")) for item in items) / count if count else 0.0
        avg_seal_amount = sum(_safe_float(item.get("seal_amount")) for item in items) / count if count else 0.0
        strong_cluster = bool(
            count >= 3
            and avg_break <= 1.5
            and (
                early_ratio >= 0.30
                or avg_seal_amount >= 120_000_000
                or avg_support >= 56.0
            )
        )
        for item in items:
            context = context_by_code.setdefault(item["code"], {})
            if count >= _safe_int(context.get("reason_theme_cohort_count")):
                context.update({
                    "reason_theme_key": theme_key,
                    "reason_theme_label": theme_label,
                    "reason_theme_cohort_count": count,
                    "reason_theme_avg_break_count": round(avg_break, 2),
                    "reason_theme_early_seal_ratio": round(early_ratio, 3),
                    "reason_theme_avg_support_strength": round(avg_support, 2),
                    "reason_theme_avg_seal_amount": round(avg_seal_amount, 2),
                    "reason_theme_strong_cluster": strong_cluster,
                })
            context["reason_cohort_count"] = max(
                _safe_int(context.get("reason_cohort_count")),
                count,
            )
            context["reason_strong_cluster"] = bool(
                context.get("reason_strong_cluster") or strong_cluster
            )
    return context_by_code


def _build_second_board_kline_shape(bar: StockKline | dict | None) -> dict:
    if bar is None:
        return {}

    getter = bar.get if isinstance(bar, dict) else lambda key, default=None: getattr(bar, key, default)
    open_price = _safe_float(getter("open"))
    high_price = _safe_float(getter("high"))
    low_price = _safe_float(getter("low"))
    close_price = _safe_float(getter("close"))
    prev_close = _safe_float(getter("prev_close"))
    if open_price <= 0 or high_price <= 0 or low_price <= 0 or close_price <= 0 or high_price < low_price:
        return {}

    open_gap_pct = max((close_price - open_price) / close_price * 100.0, 0.0)
    low_gap_pct = max((close_price - low_price) / close_price * 100.0, 0.0)
    amplitude_pct = (high_price - low_price) / prev_close * 100.0 if prev_close > 0 else 0.0
    is_one_word_shape = open_gap_pct <= 1.0 and low_gap_pct <= 1.0 and amplitude_pct <= 2.2
    is_hard_board_shape = open_gap_pct <= 3.0 and low_gap_pct <= 3.0 and amplitude_pct <= 5.0
    is_wide_divergence_shape = low_gap_pct >= 10.0 or open_gap_pct >= 10.0 or amplitude_pct >= 10.0
    if is_one_word_shape:
        shape_label = "一字/准一字"
    elif is_hard_board_shape:
        shape_label = "开盘硬板"
    elif is_wide_divergence_shape:
        shape_label = "宽幅分歧板"
    else:
        shape_label = "换手板"

    return {
        "limit_up_open_gap_pct": round(open_gap_pct, 2),
        "limit_up_low_gap_pct": round(low_gap_pct, 2),
        "limit_up_amplitude_pct": round(amplitude_pct, 2),
        "limit_up_shape_label": shape_label,
        "is_one_word_shape": is_one_word_shape,
        "is_hard_board_shape": is_hard_board_shape,
        "is_wide_divergence_shape": is_wide_divergence_shape,
    }


async def _load_second_board_kline_shape_map(
    db: AsyncSession,
    codes: list[str],
    trade_date: date,
) -> dict[str, dict]:
    if not codes:
        return {}
    result = await db.execute(
        select(StockKline).where(
            StockKline.code.in_(codes),
            StockKline.trade_date == trade_date,
        )
    )
    source_priority = {"spot_fallback": 0, "ths": 1}
    selected: dict[str, StockKline] = {}
    selected_priority: dict[str, tuple[int, int]] = {}
    for row in result.scalars().all():
        code = str(row.code or "").strip()
        if not code:
            continue
        priority = (
            source_priority.get(str(row.source or ""), 2),
            -_safe_int(getattr(row, "id", 0)),
        )
        if code not in selected_priority or priority < selected_priority[code]:
            selected[code] = row
            selected_priority[code] = priority
    return {code: _build_second_board_kline_shape(row) for code, row in selected.items()}


async def _load_second_board_position_map(
    db: AsyncSession,
    codes: list[str],
    trade_date: date,
) -> dict[str, dict]:
    """仅用首板当日之前可见的K线计算位置，避免把当日涨停后的高位当成启动前位置。"""
    if not codes:
        return {}
    result = await db.execute(
        select(
            StockKline.code,
            StockKline.trade_date,
            StockKline.close,
            StockKline.high,
            StockKline.low,
            StockKline.change_pct,
        )
        .where(
            StockKline.code.in_(codes),
            StockKline.trade_date < trade_date,
            StockKline.trade_date >= trade_date - timedelta(days=140),
        )
        .order_by(StockKline.code, StockKline.trade_date)
    )
    grouped: dict[str, list[tuple[date, float, float, float, float]]] = defaultdict(list)
    for code, bar_date, close, high, low, change_pct in result.all():
        grouped[str(code or "")].append(
            (bar_date, _safe_float(close), _safe_float(high), _safe_float(low), _safe_float(change_pct))
        )

    context: dict[str, dict] = {}
    for code, bars in grouped.items():
        valid = [bar for bar in bars if min(bar[1], bar[2], bar[3]) > 0]
        if not valid:
            continue
        pre_close = valid[-1][1]
        lookback_60 = valid[-60:]
        range_high = max(bar[2] for bar in lookback_60)
        range_low = min(bar[3] for bar in lookback_60)
        position_60 = (
            (pre_close - range_low) / (range_high - range_low)
            if range_high > range_low
            else 1.0
        )
        base_20 = valid[-20][1] if len(valid) >= 20 else valid[0][1]
        return_20 = (pre_close / base_20 - 1.0) * 100.0 if base_20 > 0 else 0.0
        recent_board_count = sum(1 for bar in valid[-20:] if bar[4] >= 8.8)
        context[code] = {
            "preboard_close": round(pre_close, 3),
            "preboard_return_20d": round(return_20, 2),
            "preboard_position_60": round(position_60, 4),
            "preboard_recent_board_count": recent_board_count,
            "preboard_low_position": bool(return_20 <= 28.0 and position_60 <= 0.78),
        }
    return context


def _build_second_board_morning_sector_context(
    first_boards: list[dict],
    sector_map: dict[str, dict],
) -> dict[str, dict]:
    grouped: dict[str, list[dict]] = {}
    for item in first_boards:
        code = str(item.get("code") or "").strip()
        if not code:
            continue
        sector = sector_map.get(code) or {}
        sector_key = str(sector.get("sector_code") or sector.get("sector_name") or "").strip()
        if not sector_key:
            continue
        minutes = _parse_limit_up_minutes(item.get("limit_up_time"))
        break_count = _safe_int(item.get("break_count"))
        seal_amount = _safe_float(item.get("seal_amount"))
        grouped.setdefault(sector_key, []).append(
            {
                "code": code,
                "sector_name": str(sector.get("sector_name") or ""),
                "minutes": minutes,
                "break_count": break_count,
                "seal_amount": seal_amount,
                "is_early": bool(minutes is not None and minutes <= 10 * 60),
                "is_opening": bool(minutes is not None and minutes <= 9 * 60 + 35),
                "is_hard": bool(
                    minutes is not None
                    and minutes <= 10 * 60
                    and break_count <= 1
                    and seal_amount >= 80_000_000
                ),
            }
        )

    context_by_code: dict[str, dict] = {}
    for items in grouped.values():
        count = len(items)
        if count <= 0:
            continue
        early_count = sum(1 for item in items if item.get("is_early"))
        opening_count = sum(1 for item in items if item.get("is_opening"))
        hard_count = sum(1 for item in items if item.get("is_hard"))
        avg_break = sum(_safe_int(item.get("break_count")) for item in items) / count
        avg_seal_amount = sum(_safe_float(item.get("seal_amount")) for item in items) / count
        early_ratio = early_count / count
        hard_ratio = hard_count / count
        score = _clamp_score(
            min(count, 6) / 6.0 * 26.0
            + early_ratio * 26.0
            + hard_ratio * 22.0
            + min(avg_seal_amount / 160_000_000, 1.0) * 16.0
            + _inverse_score(avg_break, good=0.4, bad=2.8) * 0.10
        )
        strengthening = bool(
            (count >= 3 and early_count >= 2 and (hard_count >= 1 or avg_break <= 1.4))
            or (count >= 2 and opening_count >= 2 and hard_count >= 2)
        )
        sector_name = str(items[0].get("sector_name") or "")
        payload = {
            "sector_morning_strengthening": strengthening,
            "sector_morning_strengthening_score": score,
            "sector_morning_sector_name": sector_name,
            "sector_morning_first_board_count": count,
            "sector_morning_early_count": early_count,
            "sector_morning_opening_count": opening_count,
            "sector_morning_hard_count": hard_count,
            "sector_morning_early_ratio": round(early_ratio, 3),
            "sector_morning_hard_ratio": round(hard_ratio, 3),
            "sector_morning_avg_break_count": round(avg_break, 2),
            "sector_morning_avg_seal_amount": round(avg_seal_amount, 2),
            "sector_morning_label": (
                "早盘板块强化"
                if strengthening
                else "早盘板块有扩散" if count >= 2 and early_count >= 1 else "早盘扩散一般"
            ),
        }
        for item in items:
            context_by_code[str(item.get("code") or "")] = payload
    return context_by_code


def _adjust_second_board_probability(
    base_probability: float,
    *,
    item: dict,
    sector: dict,
    market_context: dict,
    spot: StockSpot | None = None,
    fund_flow: FundFlow | None = None,
    dragon_tiger: dict | None = None,
    reason_context: dict | None = None,
    kline_shape: dict | None = None,
    morning_sector_context: dict | None = None,
) -> tuple[float, dict]:
    dragon_tiger = dragon_tiger or _dragon_tiger_empty_context()
    reason_context = reason_context or {}
    kline_shape = kline_shape or {}
    morning_sector_context = morning_sector_context or {}
    sector_continuity = _calc_sector_continuity_score(sector)
    sector_rotation_score = _safe_float(sector.get("sector_rotation_score"))
    sector_strength_delta = _safe_float(sector.get("sector_strength_delta"))
    sector_limit_up_delta = _safe_float(sector.get("sector_limit_up_delta"))
    has_low_position_rotation = bool(sector.get("sector_low_position_rotation"))
    has_crowded_stale_theme = bool(sector.get("sector_crowded_stale_theme"))
    turnover = _safe_float(item.get("turnover"))
    break_count = _safe_int(item.get("break_count"))
    seal_amount = _safe_float(item.get("seal_amount"))
    limit_up_minutes = _parse_limit_up_minutes(item.get("limit_up_time"))
    support_strength = _safe_float(getattr(spot, "support_strength_score", 0))
    circ_market_cap = _safe_float(getattr(spot, "circ_market_cap", 0))
    main_net_inflow, main_net_inflow_pct = _flow_value_from_spot_or_fund(spot, fund_flow)
    is_auction_seal = bool(limit_up_minutes is not None and limit_up_minutes <= 9 * 60 + 26)
    is_opening_hard_seal = bool(
        limit_up_minutes is not None
        and limit_up_minutes <= 9 * 60 + 35
        and break_count == 0
        and seal_amount >= 1e8
    )
    is_one_word_like = bool(
        is_auction_seal
        and break_count == 0
        and turnover <= 2.5
        and seal_amount >= 1e8
    )
    is_zero_break_hard_seal = bool(
        limit_up_minutes is not None
        and limit_up_minutes <= 9 * 60 + 45
        and break_count == 0
        and seal_amount >= 80_000_000
    )
    is_clean_early_hard_board = bool(
        limit_up_minutes is not None
        and limit_up_minutes <= 9 * 60 + 45
        and break_count <= 1
        and seal_amount >= 50_000_000
    )
    has_strong_main_inflow = bool(_safe_float(main_net_inflow) >= 50_000_000 and _safe_float(main_net_inflow_pct) >= 6.0)

    continuity_bonus = sector_continuity * 0.12
    rotation_bonus = 0.0
    rotation_penalty = 0.0
    if has_low_position_rotation:
        rotation_bonus += 0.035 + min(max(sector_rotation_score - 56.0, 0.0), 24.0) * 0.001
        if break_count == 0 and seal_amount >= 80_000_000:
            rotation_bonus += 0.012
        if limit_up_minutes is not None and limit_up_minutes <= 10 * 60:
            rotation_bonus += 0.01
    elif sector_strength_delta >= 8.0 or sector_limit_up_delta >= 2.0:
        rotation_bonus += min(max(sector_strength_delta, sector_limit_up_delta * 4.0, 0.0), 20.0) * 0.0012
    if has_crowded_stale_theme and not (is_auction_seal or is_opening_hard_seal):
        rotation_penalty += 0.035
        if turnover >= 8.0 or break_count >= 1:
            rotation_penalty += 0.018
    if is_auction_seal:
        early_seal_bonus = 0.07
    elif limit_up_minutes is not None and limit_up_minutes <= 9 * 60 + 35:
        early_seal_bonus = 0.055
    elif limit_up_minutes is not None and limit_up_minutes <= 9 * 60 + 45:
        early_seal_bonus = 0.04
    elif limit_up_minutes is not None and limit_up_minutes <= 10 * 60:
        early_seal_bonus = 0.025
    else:
        early_seal_bonus = 0.0
    seal_strength_bonus = (
        0.06
        if seal_amount >= 5e8
        else 0.04
        if seal_amount >= 2e8
        else 0.02
        if seal_amount >= 1e8
        else 0.0
    )
    turnover_bonus = 0.025 if 2 <= turnover <= 7 else 0.01 if 7 < turnover <= 10 else 0.0
    distribution_penalty = _high_board_distribution_penalty(
        market_context,
        sector_continuity=sector_continuity,
        has_high_board_risk=False,
        turnover=turnover,
        break_count=break_count,
        limit_up_minutes=limit_up_minutes,
    )

    if turnover >= 20:
        distribution_penalty += 0.07
    elif turnover >= 14:
        distribution_penalty += 0.045
    elif turnover >= 10:
        distribution_penalty += 0.025
    if _safe_float(sector.get("fund_flow")) < 0:
        distribution_penalty += 0.02
    if break_count >= 2:
        distribution_penalty += 0.04
    elif break_count == 1:
        distribution_penalty += 0.02

    dragon_tiger_delta = _safe_float(dragon_tiger.get("probability_delta"))
    style_bonus = 0.0
    style_penalty = 0.0
    if is_one_word_like:
        style_bonus += 0.10
        distribution_penalty = max(0.0, distribution_penalty - 0.06)
    elif is_opening_hard_seal:
        style_bonus += 0.07
        distribution_penalty = max(0.0, distribution_penalty - 0.035)
    elif limit_up_minutes is not None and limit_up_minutes <= 10 * 60 and break_count == 0 and seal_amount >= 1e8:
        style_bonus += 0.03
    tail_seal_penalty = 0.0
    if limit_up_minutes is not None and limit_up_minutes >= 14 * 60 + 30:
        tail_seal_penalty = 0.07
    elif limit_up_minutes is not None and limit_up_minutes >= 14 * 60:
        tail_seal_penalty = 0.04
    style_penalty += tail_seal_penalty
    if support_strength >= 85:
        style_bonus += 0.055
    elif support_strength >= 65:
        style_bonus += 0.025
    elif 0 < support_strength < 45:
        style_penalty += 0.04

    if 30 <= circ_market_cap <= 260:
        style_bonus += 0.035
    elif 0 < circ_market_cap <= 30:
        style_bonus += 0.015
    elif circ_market_cap >= 1000:
        style_penalty += 0.10
    elif circ_market_cap >= 600:
        style_penalty += 0.075
    elif circ_market_cap >= 300:
        style_penalty += 0.04

    if limit_up_minutes is not None and limit_up_minutes <= 9 * 60 + 30 and turnover <= 6:
        style_bonus += 0.035
    if _safe_float(main_net_inflow) >= 80_000_000 and _safe_float(main_net_inflow_pct) >= 8.0:
        style_bonus += 0.055
    elif _safe_float(main_net_inflow) >= 30_000_000 and _safe_float(main_net_inflow_pct) >= 4.0:
        style_bonus += 0.030
    if has_strong_main_inflow and is_clean_early_hard_board:
        style_bonus += 0.026
        distribution_penalty = max(0.0, distribution_penalty - 0.018)
    if is_zero_break_hard_seal and has_strong_main_inflow:
        style_bonus += 0.018
    if _safe_float(main_net_inflow) <= -30_000_000 or _safe_float(main_net_inflow_pct) <= -5.0:
        style_penalty += 0.04
    if _safe_int(reason_context.get("reason_cohort_count")) >= 3:
        if (
            _safe_float(reason_context.get("reason_avg_turnover")) <= 8.0
            and _safe_float(reason_context.get("reason_early_seal_ratio")) >= 0.25
            and _safe_float(reason_context.get("reason_avg_support_strength")) >= 58.0
        ):
            style_bonus += 0.035
        elif _safe_float(reason_context.get("reason_avg_break_count")) >= 2.5:
            style_penalty += 0.025
    elif bool(reason_context.get("reason_strong_cluster")):
        style_bonus += 0.018

    shape_bonus = 0.0
    shape_penalty = 0.0
    low_gap_pct = _safe_float(kline_shape.get("limit_up_low_gap_pct"))
    open_gap_pct = _safe_float(kline_shape.get("limit_up_open_gap_pct"))
    amplitude_pct = _safe_float(kline_shape.get("limit_up_amplitude_pct"))
    is_one_word_shape = bool(kline_shape.get("is_one_word_shape"))
    is_hard_board_shape = bool(kline_shape.get("is_hard_board_shape"))

    if is_one_word_shape:
        shape_bonus += 0.055
        distribution_penalty = max(0.0, distribution_penalty - 0.035)
    elif (
        is_hard_board_shape
        and limit_up_minutes is not None
        and limit_up_minutes <= 9 * 60 + 35
        and break_count == 0
    ):
        shape_bonus += 0.035
        distribution_penalty = max(0.0, distribution_penalty - 0.02)
    elif (
        low_gap_pct > 0
        and low_gap_pct <= 3.0
        and limit_up_minutes is not None
        and limit_up_minutes <= 10 * 60
        and break_count <= 1
    ):
        shape_bonus += 0.015

    if amplitude_pct >= 12.0:
        shape_penalty += 0.06
    elif amplitude_pct >= 10.0:
        shape_penalty += 0.04
    if low_gap_pct >= 10.0:
        shape_penalty += 0.045
    if open_gap_pct >= 10.0:
        shape_penalty += 0.035
    if (
        limit_up_minutes is not None
        and limit_up_minutes <= 10 * 60
        and seal_amount >= 1e8
        and turnover >= 8.0
        and not (is_one_word_shape or is_hard_board_shape)
    ):
        shape_penalty += 0.055

    morning_strengthening_bonus = 0.0
    morning_penalty_relief = 0.0
    morning_strengthening = bool(morning_sector_context.get("sector_morning_strengthening"))
    morning_score = _safe_float(morning_sector_context.get("sector_morning_strengthening_score"))
    if morning_strengthening:
        morning_strengthening_bonus += 0.024 + min(max(morning_score - 55.0, 0.0), 35.0) * 0.001
        if is_auction_seal or is_opening_hard_seal:
            morning_strengthening_bonus += 0.01
        if break_count >= 2 or tail_seal_penalty > 0:
            morning_strengthening_bonus *= 0.72
            morning_penalty_relief = 0.018
        elif seal_amount >= 80_000_000:
            morning_penalty_relief = 0.014
    elif morning_score >= 58.0:
        morning_strengthening_bonus += min(max(morning_score - 58.0, 0.0), 22.0) * 0.0007

    reason_cohort_count = _safe_int(reason_context.get("reason_cohort_count"))
    reason_strong_cluster = bool(reason_context.get("reason_strong_cluster"))
    reason_clean_cluster = bool(
        reason_cohort_count >= 3
        and _safe_float(reason_context.get("reason_avg_break_count")) <= 1.5
        and _safe_float(reason_context.get("reason_avg_turnover")) <= 10.0
    )
    low_rotation_base = bool(
        has_low_position_rotation
        or sector_limit_up_delta >= 2.0
        or sector_strength_delta >= 10.0
    )
    low_rotation_second_board = bool(
        low_rotation_base
        and (
            sector_rotation_score >= 56.0
            or sector_limit_up_delta >= 2.0
            or sector_strength_delta >= 8.0
            or is_clean_early_hard_board
        )
        and (
            reason_strong_cluster
            or reason_clean_cluster
            or morning_strengthening
            or is_auction_seal
            or is_opening_hard_seal
            or (is_clean_early_hard_board and (has_strong_main_inflow or seal_amount >= 1e8))
        )
    )
    broad_diffusion_market = bool(
        market_context.get("broad_first_board_overflow")
        or _safe_int(market_context.get("first_board_count")) >= 60
    )
    not_tail_diffusion_board = bool(limit_up_minutes is None or limit_up_minutes <= 13 * 60 + 45)
    diffusion_second_board = bool(
        broad_diffusion_market
        and not_tail_diffusion_board
        and not has_crowded_stale_theme
        and turnover <= 14.0
        and (
            sector_limit_up_delta >= 1.5
            or sector_strength_delta >= 6.0
            or reason_cohort_count >= 3
            or morning_score >= 55.0
        )
        and (
            seal_amount >= 20_000_000
            or _safe_float(main_net_inflow) >= 20_000_000
            or support_strength >= 50.0
            or is_clean_early_hard_board
        )
    )
    cluster_second_board = bool(
        (reason_strong_cluster or reason_clean_cluster)
        and (
            early_seal_bonus > 0
            or seal_amount >= 80_000_000
            or morning_strengthening
            or sector_limit_up_delta >= 2.0
        )
    )
    second_board_route = (
        "low_rotation_cluster_second_board"
        if low_rotation_second_board and cluster_second_board
        else "low_rotation_second_board"
        if low_rotation_second_board
        else "diffusion_second_board"
        if diffusion_second_board
        else "cluster_second_board"
        if cluster_second_board
        else "standard_second_board"
    )
    second_board_route_label = {
        "low_rotation_cluster_second_board": "低位轮动集群二板",
        "low_rotation_second_board": "低位轮动二板",
        "diffusion_second_board": "宽基扩散二板",
        "cluster_second_board": "集群二板",
        "standard_second_board": "常规二板晋级",
    }[second_board_route]
    rotation_cluster_bonus = 0.0
    hard_board_bonus = 0.0
    if low_rotation_second_board:
        rotation_cluster_bonus += 0.028
        if is_auction_seal or is_opening_hard_seal:
            rotation_cluster_bonus += 0.012
        if morning_strengthening:
            rotation_cluster_bonus += 0.01
        if is_clean_early_hard_board:
            rotation_cluster_bonus += 0.012
    if cluster_second_board:
        rotation_cluster_bonus += 0.018
        if reason_cohort_count >= 4:
            rotation_cluster_bonus += 0.008
    if diffusion_second_board:
        rotation_cluster_bonus += 0.016
        if sector_limit_up_delta >= 2.0:
            rotation_cluster_bonus += 0.008
        if reason_cohort_count >= 3:
            rotation_cluster_bonus += 0.006
        if morning_strengthening:
            rotation_cluster_bonus += 0.006
        if break_count >= 4:
            rotation_cluster_bonus *= 0.72
    if is_zero_break_hard_seal:
        hard_board_bonus += 0.030
    elif is_clean_early_hard_board:
        hard_board_bonus += 0.020
    if seal_amount >= 2e8 and break_count <= 1:
        hard_board_bonus += 0.016
    if has_strong_main_inflow:
        hard_board_bonus += 0.018
    hard_board_bonus = min(hard_board_bonus, 0.075)
    if rotation_cluster_bonus > 0:
        rotation_cluster_bonus = min(rotation_cluster_bonus, 0.07)

    if morning_penalty_relief > 0:
        distribution_penalty = max(0.0, distribution_penalty - morning_penalty_relief)
        if tail_seal_penalty > 0:
            style_penalty = max(0.0, style_penalty - min(tail_seal_penalty, 0.022))

    market_penalty = _market_regime_penalty(market_context, target_board=2)
    hostile_rotation_relief = 0.0
    if str(market_context.get("market_risk_level") or "") == "hostile" and rotation_cluster_bonus > 0:
        hostile_rotation_relief = min(
            market_penalty,
            0.035
            + (0.012 if low_rotation_second_board else 0.0)
            + (0.01 if cluster_second_board else 0.0)
            + (0.008 if diffusion_second_board else 0.0)
            + (0.008 if is_auction_seal or is_opening_hard_seal else 0.0),
        )
        market_penalty = max(0.0, market_penalty - hostile_rotation_relief)
    probability = (
        base_probability
        + continuity_bonus
        + early_seal_bonus
        + seal_strength_bonus
        + turnover_bonus
        + dragon_tiger_delta
        + style_bonus
        + shape_bonus
        + rotation_bonus
        + rotation_cluster_bonus
        + hard_board_bonus
        + morning_strengthening_bonus
        - distribution_penalty
        - style_penalty
        - shape_penalty
        - rotation_penalty
        - market_penalty
    )
    net_style_edge = max(
        style_bonus
        + shape_bonus
        + rotation_bonus
        + rotation_cluster_bonus
        + hard_board_bonus
        + morning_strengthening_bonus
        - style_penalty
        - shape_penalty
        - rotation_penalty,
        0.0,
    )
    if str(market_context.get("market_risk_level") or "") == "hostile":
        if low_rotation_second_board or cluster_second_board or diffusion_second_board or hard_board_bonus >= 0.045:
            hostile_cap = 0.26 + min(net_style_edge, 0.18)
            hostile_floor = min(
                hostile_cap,
                0.062
                + rotation_cluster_bonus
                + hard_board_bonus
                + (0.025 if is_auction_seal or is_opening_hard_seal else 0.0)
                + (0.012 if morning_strengthening else 0.0),
            )
            probability = max(probability, hostile_floor)
        else:
            hostile_cap = 0.16 + min(net_style_edge, 0.12)
        probability = min(probability, hostile_cap)
    elif bool(market_context.get("weak_follow_through_market")):
        weak_cap = 0.22 + min(net_style_edge, 0.10)
        if not (is_one_word_shape or is_hard_board_shape or is_opening_hard_seal):
            weak_cap -= 0.03
        probability = min(probability, weak_cap)
    style_score = _clamp_score(
        50.0
        + style_bonus * 500.0
        + shape_bonus * 500.0
        + rotation_bonus * 420.0
        + rotation_cluster_bonus * 420.0
        + hard_board_bonus * 420.0
        + morning_strengthening_bonus * 420.0
        - style_penalty * 450.0
        - shape_penalty * 450.0
        - rotation_penalty * 420.0
        - distribution_penalty * 120.0
        - market_penalty * 80.0
    )
    return _clamp_probability(probability), {
        "sector_continuity_score": round(sector_continuity, 3),
        "distribution_penalty": round(distribution_penalty, 3),
        "early_seal_bonus": round(early_seal_bonus, 3),
        "seal_strength_bonus": round(seal_strength_bonus, 3),
        "turnover_bonus": round(turnover_bonus, 3),
        "style_bonus": round(style_bonus, 3),
        "style_penalty": round(style_penalty, 3),
        "shape_bonus": round(shape_bonus, 3),
        "shape_penalty": round(shape_penalty, 3),
        "rotation_bonus": round(rotation_bonus, 3),
        "rotation_cluster_bonus": round(rotation_cluster_bonus, 3),
        "hard_board_bonus": round(hard_board_bonus, 3),
        "rotation_penalty": round(rotation_penalty, 3),
        "morning_strengthening_bonus": round(morning_strengthening_bonus, 3),
        "morning_penalty_relief": round(morning_penalty_relief, 3),
        "hostile_rotation_relief": round(hostile_rotation_relief, 3),
        "second_board_route": second_board_route,
        "second_board_route_label": second_board_route_label,
        "low_rotation_second_board": low_rotation_second_board,
        "cluster_second_board": cluster_second_board,
        "diffusion_second_board": diffusion_second_board,
        "sector_rotation_score": round(sector_rotation_score, 2),
        "sector_strength_delta": round(sector_strength_delta, 2),
        "sector_limit_up_delta": round(sector_limit_up_delta, 2),
        "sector_low_position_rotation": has_low_position_rotation,
        "sector_crowded_stale_theme": has_crowded_stale_theme,
        "sector_rotation_label": str(sector.get("sector_rotation_label") or ""),
        **kline_shape,
        "tail_seal_penalty": round(tail_seal_penalty, 3),
        "limit_up_minutes": limit_up_minutes,
        "is_auction_seal": is_auction_seal,
        "is_opening_hard_seal": is_opening_hard_seal,
        "is_one_word_like": is_one_word_like,
        "is_zero_break_hard_seal": is_zero_break_hard_seal,
        "is_clean_early_hard_board": is_clean_early_hard_board,
        "has_strong_main_inflow": has_strong_main_inflow,
        "seal_amount": round(seal_amount, 2),
        "second_board_style_score": style_score,
        "support_strength_score": round(support_strength, 2),
        "circ_market_cap": round(circ_market_cap, 2),
        "main_net_inflow": round(main_net_inflow, 2) if main_net_inflow is not None else None,
        "main_net_inflow_pct": round(main_net_inflow_pct, 2) if main_net_inflow_pct is not None else None,
        **reason_context,
        "market_regime_penalty": round(market_penalty, 3),
        "market_risk_level": str(market_context.get("market_risk_level") or ""),
        "market_sentiment_cycle": str(market_context.get("sentiment_cycle") or ""),
        "market_main_net_inflow": _safe_float(market_context.get("main_net_inflow")),
        "market_weak_follow_through": bool(market_context.get("weak_follow_through_market")),
        "market_broad_first_board_overflow": bool(market_context.get("broad_first_board_overflow")),
        "market_avg_break_count": round(_safe_float(market_context.get("avg_break_count")), 3),
        "market_broken_ratio": round(_safe_float(market_context.get("broken_ratio")), 3),
        "market_first_board_count": _safe_int(market_context.get("first_board_count")),
        "dragon_tiger_listed": bool(dragon_tiger.get("is_listed")),
        "dragon_tiger_score": _safe_float(dragon_tiger.get("member_score"), 50.0),
        "dragon_tiger_probability_delta": round(dragon_tiger_delta, 3),
        "dragon_tiger_summary": str(dragon_tiger.get("summary") or ""),
        **morning_sector_context,
    }


async def _build_first_board_candidates(
    db: AsyncSession,
    trade_date: date,
    limit_up_codes: set[str],
    market_context: dict,
    limit: int,
    snapshot: dict | None = None,
    news_end_time: datetime | None = None,
) -> tuple[list[dict], str | None, dict]:
    snapshot = snapshot or await prewarm_anomaly_snapshot(db, trade_date=trade_date)
    rows = [
        row
        for row in _build_stock_rows(snapshot.get("anomalies") or [], sort_by="priority")
        if stock_tagger.is_tradeable(str(row.get("code") or "").strip())
    ]
    snapshot_time = _resolve_snapshot_clock_source(snapshot, rows)
    snapshot_as_of_date, snapshot_session_name = _resolve_signal_clock(snapshot_time, trade_date)
    bull_rank = await _bull_rank(db)
    bull_rank_rows = bull_rank.get("rank") or []
    bull_map = {str(item.get("code") or ""): item for item in bull_rank_rows}
    full_snapshot_scan = limit >= PROMOTION_INTERNAL_RECORD_LIMIT
    quiet_scan_limit = 1600 if full_snapshot_scan else max(320, min(1600, limit * 4))
    hot_mainline_scan_limit = (
        HOT_MAINLINE_RELAY_SCAN_LIMIT
        if full_snapshot_scan
        else max(240, min(HOT_MAINLINE_RELAY_SCAN_LIMIT, limit * 3))
    )
    broad_rotation_scan_limit = (
        BROAD_ROTATION_FIRST_BOARD_SCAN_LIMIT
        if full_snapshot_scan
        else max(260, min(BROAD_ROTATION_FIRST_BOARD_SCAN_LIMIT, limit * 4))
    )
    pre_board_scan_limit = (
        PRE_BOARD_SCAN_LIMIT
        if full_snapshot_scan
        else max(360, min(PRE_BOARD_SCAN_LIMIT, limit * 5))
    )
    quiet_spots = await _load_quiet_setup_spots(
        db,
        exclude_codes=limit_up_codes,
        limit=quiet_scan_limit,
    )
    quiet_codes = [str(item.code or "").strip() for item in quiet_spots if str(item.code or "").strip()]
    row_codes = {
        str(row.get("code") or "").strip()
        for row in rows
        if str(row.get("code") or "").strip()
    }
    hot_mainline_spots = await _load_hot_mainline_relay_spots(
        db,
        trade_date,
        exclude_codes=limit_up_codes,
        market_context=market_context,
        limit=hot_mainline_scan_limit,
    )
    hot_mainline_spot_map = {
        str(item.code or "").strip(): item
        for item in hot_mainline_spots
        if str(item.code or "").strip()
    }
    hot_mainline_codes = [
        str(item.code or "").strip()
        for item in hot_mainline_spots
        if str(item.code or "").strip()
    ]
    broad_rotation_spots = await _load_broad_rotation_first_board_spots(
        db,
        trade_date,
        exclude_codes=limit_up_codes,
        market_context=market_context,
        limit=broad_rotation_scan_limit,
    )
    hot_mainline_code_set = set(hot_mainline_codes)
    broad_rotation_spots = [
        item
        for item in broad_rotation_spots
        if str(item.code or "").strip() not in hot_mainline_code_set
    ]
    broad_rotation_spot_map = {
        str(item.code or "").strip(): item
        for item in broad_rotation_spots
        if str(item.code or "").strip()
    }
    broad_rotation_codes = [
        str(item.code or "").strip()
        for item in broad_rotation_spots
        if str(item.code or "").strip()
    ]
    resolved_news_end_time = (
        news_end_time
        if news_end_time is not None
        else await _resolve_live_promotion_news_end_time(trade_date)
    )
    news_catalyst_map = await _load_news_catalyst_context_map(
        db,
        trade_date,
        exclude_codes=limit_up_codes,
        limit=pre_board_scan_limit,
        news_end_time=resolved_news_end_time,
    )
    news_catalyst_map = {
        code: {
            **context,
            "news_query_end_time": (
                resolved_news_end_time.isoformat(timespec="seconds")
                if isinstance(resolved_news_end_time, datetime)
                else ""
            ),
        }
        for code, context in news_catalyst_map.items()
        if stock_tagger.is_tradeable(code)
    }
    auction_surge_map = await _load_auction_surge_context_map(
        db,
        trade_date,
        exclude_codes=limit_up_codes,
    )
    pre_board_probe_map = await _load_pre_board_probe_context_map(
        db,
        trade_date,
        exclude_codes=limit_up_codes,
        market_context=market_context,
    )
    context_codes = list(dict.fromkeys([*news_catalyst_map.keys(), *auction_surge_map.keys(), *pre_board_probe_map.keys()]))
    codes = list(
        dict.fromkeys(
            [
                str(row.get("code") or "").strip()
                for row in rows
                if str(row.get("code") or "").strip()
            ]
            + quiet_codes
            + hot_mainline_codes
            + broad_rotation_codes
            + context_codes
        )
    )
    sector_hint_by_code = {
        code: "+".join(
            str(value or "").strip()
            for value in (context.get("news_related_sectors") or [])
            if str(value or "").strip()
        )
        for code, context in news_catalyst_map.items()
        if context.get("news_related_sectors")
    }
    for code, context in auction_surge_map.items():
        auction_sector_name = str(context.get("auction_sector_name") or "").strip()
        if (
            auction_sector_name
            and bool(context.get("auction_cluster_confirmed"))
            and _safe_int(context.get("auction_sector_member_count")) >= 3
        ):
            existing_hint = str(sector_hint_by_code.get(code) or "").strip()
            sector_hint_by_code[code] = "+".join(
                part for part in (existing_hint, auction_sector_name) if part
            )
    sector_map = await _load_best_sector_context(
        db,
        trade_date,
        codes,
        reason_by_code=sector_hint_by_code,
    )
    # 主营行业证据独立保留，不能被几十个宽泛概念中当天最热的一个覆盖。
    # 历史对照中“行业低位轮动 + 个股修复活跃”的首板 lift 明显高于
    # 全概念最优匹配，因此只用 pywencai industry 口径构造组合特征。
    primary_industry_map = await _load_best_sector_context(
        db,
        trade_date,
        codes,
        allowed_sector_types={"industry"},
    )
    auction_sector_hint_by_code = {
        code: str(context.get("auction_sector_name") or "").strip()
        for code, context in auction_surge_map.items()
        if str(context.get("auction_sector_name") or "").strip()
        and bool(context.get("auction_cluster_confirmed"))
        and _safe_int(context.get("auction_sector_member_count")) >= 3
    }
    auction_sector_map = await _load_best_sector_context(
        db,
        trade_date,
        list(auction_sector_hint_by_code),
        reason_by_code=auction_sector_hint_by_code,
    ) if auction_sector_hint_by_code else {}
    kline_map = await _load_first_board_kline_context(
        db,
        codes,
        trade_date,
        # 约一年完整交易日；盘中只把已完成K线送入长周期画像。
        lookback_days=420 if full_snapshot_scan else 360,
        as_of_date=snapshot_as_of_date,
        session_name=snapshot_session_name,
    )
    spot_map = await _load_spot_map(db, codes)
    fund_flow_map = await _load_fund_flow_map(db, codes, trade_date)
    fund_flow_trend_map = await _load_fund_flow_trend_map(db, codes, trade_date)
    memory_map = await _load_recent_limit_up_memory(db, codes, trade_date)

    hot_rows: list[dict] = []
    broad_rotation_rows: list[dict] = []
    hot_mainline_row_map: dict[str, dict] = {}
    broad_rotation_row_map: dict[str, dict] = {}
    seen_hot_codes: set[str] = set()
    for spot in hot_mainline_spots:
        code = str(spot.code or "").strip()
        if not code or code in seen_hot_codes or code in limit_up_codes:
            continue
        sector = sector_map.get(code, {})
        if (
            _safe_float(sector.get("strength_score")) < 1
            and not bool(sector.get("sector_washout_reversal_watch"))
        ):
            continue
        kline_context = kline_map.get(code, {})
        if not kline_context or bool(kline_context.get("insufficient_history")):
            continue
        live_spot = _select_spot_for_trade_date(spot_map.get(code), spot, trade_date)
        fund_flow = fund_flow_map.get(code)
        bull_row = _build_hot_mainline_relay_bull_row(
            live_spot,
            sector,
            kline_context,
            fund_flow=fund_flow,
            existing_row=bull_map.get(code),
        )
        hot_row = _build_hot_mainline_relay_row(
            live_spot,
            sector=sector,
            bull_row=bull_row,
            kline_context=kline_context,
            fund_flow=fund_flow,
            market_context=market_context,
        )
        hot_mainline_row_map[code] = hot_row
        hot_rows.append(hot_row)
        bull_map.setdefault(code, bull_row)
        seen_hot_codes.add(code)

    seen_broad_rotation_codes: set[str] = set()
    for spot in broad_rotation_spots:
        code = str(spot.code or "").strip()
        if not code or code in seen_broad_rotation_codes or code in limit_up_codes:
            continue
        sector = sector_map.get(code, {})
        if (
            _safe_float(sector.get("strength_score")) < 1
            and not bool(sector.get("sector_washout_reversal_watch"))
        ):
            continue
        kline_context = kline_map.get(code, {})
        if not kline_context or bool(kline_context.get("insufficient_history")):
            continue
        live_spot = _select_spot_for_trade_date(spot_map.get(code), spot, trade_date)
        fund_flow = fund_flow_map.get(code)
        bull_row = _build_broad_rotation_first_board_bull_row(
            live_spot,
            sector,
            kline_context,
            fund_flow=fund_flow,
            existing_row=bull_map.get(code),
        )
        broad_row = _build_broad_rotation_first_board_row(
            live_spot,
            sector=sector,
            bull_row=bull_row,
            kline_context=kline_context,
            fund_flow=fund_flow,
            market_context=market_context,
        )
        broad_rotation_row_map[code] = broad_row
        broad_rotation_rows.append(broad_row)
        bull_map.setdefault(code, bull_row)
        seen_broad_rotation_codes.add(code)

    if news_catalyst_map or auction_surge_map or pre_board_probe_map or hot_mainline_row_map or broad_rotation_row_map:
        enriched_rows: list[dict] = []
        for row in rows:
            code = str(row.get("code") or "").strip()
            enriched_rows.append(
                _enrich_first_board_row_with_context(
                    row,
                    news_context=news_catalyst_map.get(code),
                    auction_context=auction_surge_map.get(code),
                    pre_board_context=pre_board_probe_map.get(code),
                    hot_mainline_row=hot_mainline_row_map.get(code) or broad_rotation_row_map.get(code),
                )
            )
        rows = enriched_rows

    context_rows: list[dict] = []
    seen_context_codes: set[str] = set(row_codes)
    for code, auction_context in auction_surge_map.items():
        if not code or code in seen_context_codes or code in limit_up_codes:
            continue
        live_spot = spot_map.get(code)
        if live_spot is None or _safe_float(live_spot.price) <= 0:
            continue
        sector = auction_sector_map.get(code) or sector_map.get(code, {})
        fund_flow = fund_flow_map.get(code)
        bull_row = _build_auction_surge_bull_row(
            live_spot,
            sector,
            auction_context,
            fund_flow=fund_flow,
            existing_row=bull_map.get(code),
        )
        context_rows.append(
            _enrich_first_board_row_with_context(
                _build_auction_surge_row(
                    live_spot,
                    sector=sector,
                    bull_row=bull_row,
                    auction_context=auction_context,
                    fund_flow=fund_flow,
                ),
                news_context=news_catalyst_map.get(code),
                pre_board_context=pre_board_probe_map.get(code),
            )
        )
        bull_map.setdefault(code, bull_row)
        seen_context_codes.add(code)

    for code, news_context in news_catalyst_map.items():
        if not code or code in seen_context_codes or code in limit_up_codes:
            continue
        live_spot = spot_map.get(code)
        if live_spot is None or _safe_float(live_spot.price) <= 0:
            continue
        sector = sector_map.get(code, {})
        fund_flow = fund_flow_map.get(code)
        bull_row = _build_news_catalyst_bull_row(
            live_spot,
            sector,
            news_context,
            fund_flow=fund_flow,
            existing_row=bull_map.get(code),
        )
        context_rows.append(
            _enrich_first_board_row_with_context(
                _build_news_catalyst_row(
                    live_spot,
                    sector=sector,
                    bull_row=bull_row,
                    news_context=news_context,
                    fund_flow=fund_flow,
                ),
                auction_context=auction_surge_map.get(code),
                pre_board_context=pre_board_probe_map.get(code),
            )
        )
        bull_map.setdefault(code, bull_row)
        seen_context_codes.add(code)

    for code, pre_board_context in pre_board_probe_map.items():
        if not code or code in seen_context_codes or code in limit_up_codes:
            continue
        sector = sector_map.get(code, {})
        fund_flow = fund_flow_map.get(code)
        bull_row = _build_pre_board_probe_bull_row(
            pre_board_context,
            sector,
            existing_row=bull_map.get(code),
        )
        context_rows.append(
            _build_pre_board_probe_row(
                pre_board_context,
                sector=sector,
                bull_row=bull_row,
                fund_flow=fund_flow,
            )
        )
        bull_map.setdefault(code, bull_row)
        seen_context_codes.add(code)
    if hot_rows or broad_rotation_rows or context_rows:
        rows = list(rows) + hot_rows + broad_rotation_rows + context_rows

    # 在统一候选入口挂接启动前研究特征，保证异常、主线、消息、竞价和
    # K线宽召回路线使用同一 as-of 资金/行业口径。该证据只改变预测排序，
    # ``trade_ready`` 仍由后面的严格量价与执行闸门独立决定。
    default_funding_context = build_funding_preheat_context([])
    launch_enriched_rows: list[dict] = []
    for row in rows:
        code = str(row.get("code") or "").strip()
        funding_context = fund_flow_trend_map.get(code) or default_funding_context
        ignition_context = build_low_base_sector_ignition_context(
            kline_map.get(code, {}),
            funding_context,
            primary_industry_map.get(code),
            news=news_catalyst_map.get(code),
        )
        detail = {
            **(row.get("detail") or {}),
            **funding_context,
            **ignition_context,
            "launch_feature_version": LAUNCH_PRECURSOR_FEATURE_VERSION,
        }
        event_types = list(row.get("event_types") or [])
        if bool(ignition_context.get("low_base_sector_ignition_ready")):
            event_types.append("low_base_sector_ignition")
            detail["prediction_shape_seed"] = True
        secondary_reason = str(row.get("secondary_reason") or "")
        if bool(ignition_context.get("low_base_sector_ignition_ready")):
            industry_name = str(ignition_context.get("primary_industry_name") or "主营行业")
            ignition_note = (
                f"{industry_name}低位点火"
                f" · 三日资金{_safe_float(funding_context.get('funding_main_inflow_pct_3d')):+.1f}%"
            )
            secondary_reason = " · ".join(
                item for item in (secondary_reason, ignition_note) if item
            )
        launch_enriched_rows.append({
            **row,
            "event_types": list(dict.fromkeys(str(item or "") for item in event_types if str(item or ""))),
            "detail": detail,
            "secondary_reason": secondary_reason,
        })
    rows = launch_enriched_rows

    candidates: list[dict] = []
    blocked_diagnostics: dict[str, dict] = {}
    for row in rows:
        code = str(row.get("code") or "").strip()
        if not code or code in limit_up_codes:
            continue

        event_types = set(str(item or "") for item in row.get("event_types") or [])
        detail = row.get("detail") or {}
        change_pct = _safe_float(detail.get("change_pct"))
        latest_as_of = str(row.get("latest_as_of") or detail.get("as_of") or "")
        sector = (
            auction_sector_map.get(code)
            if "auction_surge" in event_types and bool(detail.get("auction_cluster_confirmed"))
            else None
        ) or sector_map.get(code, {})
        sector_continuity_score = _calc_sector_continuity_score(sector)
        memory_features = memory_map.get(code, {})
        live_spot = spot_map.get(code)
        bull_row = _build_contextual_first_board_bull_row(
            row=row,
            spot=live_spot,
            sector=sector,
            kline_context=kline_map.get(code, {}),
            news_context=news_catalyst_map.get(code),
            auction_context=auction_surge_map.get(code),
            pre_board_context=pre_board_probe_map.get(code),
            hot_mainline_spot=hot_mainline_spot_map.get(code) or broad_rotation_spot_map.get(code),
            fund_flow=fund_flow_map.get(code),
            existing_row=bull_map.get(code),
        )
        if bull_row:
            bull_map.setdefault(code, bull_row)
        bull_score = _safe_float(bull_row.get("total_score"))
        kline_context = kline_map.get(code, {})
        diagnostic_route = _first_board_diagnostic_route(event_types)
        signal_summary = " / ".join((row.get("event_types") or [])[:3]) or str(row.get("primary_reason") or "")
        if not _should_collect_first_board_diagnostic_from_events(event_types):
            continue

        min_candidate_change_pct = 1.0
        if "news_catalyst" in event_types:
            min_candidate_change_pct = -1.0
        elif "auction_surge" in event_types:
            min_candidate_change_pct = 0.0
        elif "mainline_spread" in event_types:
            min_candidate_change_pct = (
                SECTOR_WASHOUT_WATCH_MIN_CHANGE_PCT
                if bool(detail.get("sector_washout_reversal_watch"))
                else BROAD_ROTATION_FIRST_BOARD_MIN_CHANGE_PCT
                if bool(detail.get("broad_rotation_cluster_setup")) or bool(detail.get("sector_catalyst_spread"))
                else -2.5
                if bool(detail.get("broad_rotation_member_setup"))
                else MAINLINE_SPREAD_MIN_CHANGE_PCT
            )
        elif "pre_board_probe" in event_types:
            min_candidate_change_pct = -2.0
        elif "oversold_reversal" in event_types:
            min_candidate_change_pct = -10.5
        if change_pct < min_candidate_change_pct or change_pct >= 9.7:
            _record_first_board_diagnostic(
                blocked_diagnostics,
                code=code,
                name=str(row.get("name") or "").strip(),
                blockers=["涨幅区间不符合首板点火"],
                signal_summary=signal_summary,
                trigger_reason=str(row.get("primary_reason") or ""),
                sector_name=str(sector.get("sector_name") or row.get("driver_primary") or ""),
                latest_as_of=latest_as_of,
                change_pct=change_pct,
                volume_ratio=_safe_float(detail.get("volume_ratio")),
                support_strength_score=_safe_float(detail.get("support_strength_score")),
                sector_strength_score=_safe_float(sector.get("strength_score")),
                sector_continuity_score=sector_continuity_score,
                memory_score=_safe_float(memory_features.get("memory_score")),
                bull_score=bull_score,
                main_net_inflow_pct=_safe_float(detail.get("main_net_inflow_pct")),
                kline_confirmation_score=_safe_float(kline_context.get("confirmation_score")),
                strict_confirmation_count=_safe_int(kline_context.get("strict_confirmation_count")),
                overhead_gap_pct=_safe_float(kline_context.get("overhead_gap_pct")),
                amplitude=_safe_float(detail.get("amplitude")),
                provisional_last_bar=bool(kline_context.get("provisional_last_bar")),
                diagnostic_route=diagnostic_route,
            )
            continue

        setup_grade = str(row.get("setup_grade") or "")
        min_prefilter_bull_score = 55.0
        if "news_catalyst" in event_types:
            min_prefilter_bull_score = 48.0
        elif "auction_surge" in event_types:
            min_prefilter_bull_score = 46.0
        elif "mainline_spread" in event_types:
            min_prefilter_bull_score = 50.0
        elif "pre_board_probe" in event_types:
            min_prefilter_bull_score = 48.0
        elif "oversold_reversal" in event_types:
            min_prefilter_bull_score = 46.0
        if (
            bull_score < min_prefilter_bull_score
            and SETUP_GRADE_RANK.get(setup_grade, -1) < SETUP_GRADE_RANK["A2 盘口确认后执行"]
            and not bool(detail.get("prediction_shape_seed"))
        ):
            _record_first_board_diagnostic(
                blocked_diagnostics,
                code=code,
                name=str(row.get("name") or "").strip(),
                blockers=["牛股分不足"],
                signal_summary=signal_summary,
                trigger_reason=str(row.get("primary_reason") or ""),
                sector_name=str(sector.get("sector_name") or row.get("driver_primary") or ""),
                latest_as_of=latest_as_of,
                change_pct=change_pct,
                volume_ratio=_safe_float(detail.get("volume_ratio")),
                support_strength_score=_safe_float(detail.get("support_strength_score")),
                sector_strength_score=_safe_float(sector.get("strength_score")),
                sector_continuity_score=sector_continuity_score,
                memory_score=_safe_float(memory_features.get("memory_score")),
                bull_score=bull_score,
                main_net_inflow_pct=_safe_float(detail.get("main_net_inflow_pct")),
                kline_confirmation_score=_safe_float(kline_context.get("confirmation_score")),
                strict_confirmation_count=_safe_int(kline_context.get("strict_confirmation_count")),
                overhead_gap_pct=_safe_float(kline_context.get("overhead_gap_pct")),
                amplitude=_safe_float(detail.get("amplitude")),
                provisional_last_bar=bool(kline_context.get("provisional_last_bar")),
                diagnostic_route=diagnostic_route,
            )
            continue

        strict_pass, strict_blockers, strict_route = _passes_strict_first_board_gate(
            row,
            bull_score=bull_score,
            sector=sector,
            kline_context=kline_context,
            memory_features=memory_features,
            market_context=market_context,
        )
        if not strict_pass:
            _record_first_board_diagnostic(
                blocked_diagnostics,
                code=code,
                name=str(row.get("name") or "").strip(),
                blockers=strict_blockers,
                signal_summary=signal_summary,
                trigger_reason=str(row.get("primary_reason") or ""),
                sector_name=str(sector.get("sector_name") or row.get("driver_primary") or ""),
                latest_as_of=latest_as_of,
                change_pct=change_pct,
                volume_ratio=_safe_float(detail.get("volume_ratio")),
                support_strength_score=_safe_float(detail.get("support_strength_score")),
                sector_strength_score=_safe_float(sector.get("strength_score")),
                sector_continuity_score=sector_continuity_score,
                memory_score=_safe_float(memory_features.get("memory_score")),
                bull_score=bull_score,
                main_net_inflow_pct=_safe_float(detail.get("main_net_inflow_pct")),
                kline_confirmation_score=_safe_float(kline_context.get("confirmation_score")),
                strict_confirmation_count=_safe_int(kline_context.get("strict_confirmation_count")),
                overhead_gap_pct=_safe_float(kline_context.get("overhead_gap_pct")),
                amplitude=_safe_float(detail.get("amplitude")),
                provisional_last_bar=bool(kline_context.get("provisional_last_bar")),
                diagnostic_route=strict_route or diagnostic_route,
            )
            relaxed_probability, relaxed_probability_factors = _build_first_board_probability(
                row,
                bull_row,
                sector,
                market_context,
                kline_context=kline_context,
                memory_features=memory_features,
                candidate_route=_candidate_route_from_strict_route(
                    strict_route,
                    sector=sector,
                    bull_score=bull_score,
                    support_strength=_safe_float(detail.get("support_strength_score")),
                ),
            )
            adjusted_probability, relaxed_meta = _adjust_relaxed_first_board_probability(
                relaxed_probability,
                strict_blockers=strict_blockers,
                route=str(strict_route or diagnostic_route or ""),
            )
            is_washout_seed = bool(
                detail.get("sector_washout_reversal_watch")
                and detail.get("prediction_shape_seed")
            )
            auction_cluster_confirmed = bool(detail.get("auction_cluster_confirmed"))
            auction_feed_complete = auction_context_complete(detail)
            prediction_watch_cap = _prediction_shape_watch_probability_cap(detail)
            if prediction_watch_cap > 0:
                adjusted_probability = min(adjusted_probability, prediction_watch_cap)
            adjusted_sub_probabilities = {
                key: _clamp_probability(max(0.05, _safe_float(value) - _safe_float(relaxed_meta.get("sub_probability_penalty"))))
                for key, value in (relaxed_probability_factors.get("sub_probabilities") or {}).items()
            }
            adjusted_sub_probabilities["first_limitup_next_day"] = adjusted_probability
            relaxed_probability_factors = {
                **relaxed_probability_factors,
                "route_score": _clamp_score(
                    _safe_float(relaxed_probability_factors.get("route_score"))
                    - _safe_float(relaxed_meta.get("route_score_penalty"))
                ),
                "sub_probabilities": adjusted_sub_probabilities,
                "relaxed_watch_penalty": _safe_float(relaxed_meta.get("penalty")),
                "relaxed_watch_blocker_categories": relaxed_meta.get("blocker_categories") or [],
                "strict_gate_status": "watch_relaxed",
                "prediction_watch_probability_cap": round(prediction_watch_cap, 3),
            }
            if _should_keep_relaxed_first_board_watch(
                row,
                bull_score=bull_score,
                sector=sector,
                kline_context=kline_context,
                memory_features=memory_features,
                probability=adjusted_probability,
                probability_factors=relaxed_probability_factors,
                strict_blockers=strict_blockers,
            ) or bool(detail.get("prediction_shape_seed")) or _is_fresh_direct_hard_news_context(detail):
                sector_reason = str(sector.get("sector_name") or row.get("driver_primary") or "")
                kline_reason = str(kline_context.get("summary") or "")
                blocker_reason = str(strict_blockers[0] if strict_blockers else "未进冲刺主池")
                signal_state = _resolve_promotion_signal_status(
                    target_board=1,
                    trade_date=trade_date,
                    latest_as_of=str(row.get("latest_as_of") or ""),
                    kline_context=kline_context,
                    today=_resolve_signal_clock(str(row.get("latest_as_of") or snapshot_time), trade_date)[0],
                    session_name=_resolve_signal_clock(str(row.get("latest_as_of") or snapshot_time), trade_date)[1],
                )
                candidates.append(
                    _build_first_board_candidate_item(
                        code=code,
                        name=str(row.get("name") or "").strip(),
                        probability=adjusted_probability,
                        probability_factors=relaxed_probability_factors,
                        setup_grade=setup_grade,
                        setup_grade_display=str(row.get("setup_grade_display") or setup_grade),
                        signal_summary=" / ".join((row.get("event_types") or [])[:3]),
                        primary_reason=str(row.get("primary_reason") or ""),
                        secondary_reason=" · ".join(
                            item for item in [f"未进冲刺主池：{blocker_reason}", kline_reason] if item
                        ),
                        detail=detail,
                        display_score=_safe_float(row.get("display_score")),
                        bull_row=bull_row,
                        bull_score=bull_score,
                        sector=sector,
                        sector_reason=sector_reason,
                        event_types=list(event_types),
                        memory_features=memory_features,
                        kline_context=kline_context,
                        risk_flags=row.get("risk_flags") or [],
                        strict_blockers=strict_blockers,
                        prediction_mode=strict_route or diagnostic_route or "strict",
                        latest_as_of=str(row.get("latest_as_of") or ""),
                        signal_state=signal_state,
                        time_horizon="watch",
                        time_horizon_label=(
                            "竞价集群观察"
                            if auction_cluster_confirmed
                            else "洗盘反转观察"
                            if is_washout_seed
                            else "首板梯队"
                        ),
                        time_horizon_reason=(
                            "最新竞价帧出现同板块集体转强，但增量成交数据不完整；"
                            "只做重点预测观察，开盘量价确认前仓位为0"
                            if auction_cluster_confirmed and not auction_feed_complete
                            else "最新竞价帧出现同板块集体转强且成交增量完整；"
                            "仍因严格闸门未通过而只观察，等待开盘承接确认"
                            if auction_cluster_confirmed
                            else
                            "近期活跃主线单日急跌但仍保留涨停火种；先进入次日反转观察池，"
                            "只有竞价同板块集体转强或盘中放量承接确认后才升格"
                            if is_washout_seed
                            else "未达到 1-2 日冲刺标准，但仍保留在首板预测梯队中继续跟踪"
                        ),
                        keep_in_diagnostics=True,
                        trade_ready=False,
                        strict_gate_passed=False,
                    )
                )
            continue

        probability, probability_factors = _build_first_board_probability(
            row,
            bull_row,
            sector,
            market_context,
            kline_context=kline_context,
            memory_features=memory_features,
            candidate_route=_candidate_route_from_strict_route(
                strict_route,
                sector=sector,
                bull_score=bull_score,
                support_strength=_safe_float(detail.get("support_strength_score")),
            ),
        )
        sector_reason = str(sector.get("sector_name") or row.get("driver_primary") or "")
        kline_reason = str(kline_context.get("summary") or "")
        signal_state = _resolve_promotion_signal_status(
            target_board=1,
            trade_date=trade_date,
            latest_as_of=str(row.get("latest_as_of") or ""),
            kline_context=kline_context,
            today=_resolve_signal_clock(str(row.get("latest_as_of") or snapshot_time), trade_date)[0],
            session_name=_resolve_signal_clock(str(row.get("latest_as_of") or snapshot_time), trade_date)[1],
        )
        candidates.append(
            _build_first_board_candidate_item(
                code=code,
                name=str(row.get("name") or "").strip(),
                probability=probability,
                probability_factors=probability_factors,
                setup_grade=setup_grade,
                setup_grade_display=str(row.get("setup_grade_display") or setup_grade),
                signal_summary=" / ".join((row.get("event_types") or [])[:3]),
                primary_reason=str(row.get("primary_reason") or ""),
                secondary_reason=" · ".join(
                    item for item in [str(row.get("secondary_reason") or ""), kline_reason] if item
                ),
                detail=detail,
                display_score=_safe_float(row.get("display_score")),
                bull_row=bull_row,
                bull_score=bull_score,
                sector=sector,
                sector_reason=sector_reason,
                event_types=list(event_types),
                memory_features=memory_features,
                kline_context=kline_context,
                risk_flags=row.get("risk_flags") or [],
                strict_blockers=strict_blockers,
                prediction_mode=strict_route or "strict",
                latest_as_of=str(row.get("latest_as_of") or ""),
                signal_state=signal_state,
                time_horizon="watch" if bool(detail.get("prediction_shape_seed")) else None,
                time_horizon_label=(
                    "竞价集群观察"
                    if bool(detail.get("auction_cluster_confirmed"))
                    else "启动形态预测"
                    if bool(detail.get("prediction_shape_seed"))
                    else None
                ),
                time_horizon_reason=(
                    "竞价出现同板块集体转强，但成交增量未完整；只进入预测观察池，确认买点前仓位为0"
                    if bool(detail.get("auction_cluster_confirmed")) and not auction_context_complete(detail)
                    else "竞价出现同板块集体转强且成交增量完整；仍需开盘承接确认，确认买点前仓位为0"
                    if bool(detail.get("auction_cluster_confirmed"))
                    else "近期活跃主线单日急跌但仍保留涨停火种；先观察竞价集体修复，确认买点前仓位为0"
                    if bool(detail.get("sector_washout_reversal_watch"))
                    else "中期动量后缩量洗盘，仅进入预测与盘中检测池；次日确认买点前仓位为0"
                    if bool(detail.get("prediction_shape_seed"))
                    else None
                ),
                keep_in_diagnostics=bool(detail.get("prediction_shape_seed")),
                trade_ready=not bool(detail.get("prediction_shape_seed")),
            )
        )

    existing_codes = {str(item.get("code") or "").strip() for item in candidates if item.get("code")}
    for spot in quiet_spots:
        code = str(spot.code or "").strip()
        if not code or code in limit_up_codes or code in existing_codes:
            continue

        live_spot = spot_map.get(code) or spot
        if not live_spot or _safe_float(live_spot.price) <= 0:
            continue

        kline_context = kline_map.get(code, {})
        sector = sector_map.get(code, {})
        sector_continuity_score = _calc_sector_continuity_score(sector)
        quiet_memory_features = memory_map.get(code) or {}
        quiet_bull_row = bull_map.get(code) or {}
        if not kline_context or bool(kline_context.get("insufficient_history")):
            _record_first_board_diagnostic(
                blocked_diagnostics,
                code=code,
                name=str(spot.name or code).strip(),
                blockers=["K线历史不足"],
                signal_summary="静默蓄势 / 多日形态确认",
                trigger_reason="静默蓄势扫描",
                sector_name=str((sector_map.get(code) or {}).get("sector_name") or ""),
                latest_as_of=str(getattr(live_spot, "updated_at", "") or ""),
                change_pct=_safe_float(getattr(live_spot, "change_pct", 0.0)),
                volume_ratio=_safe_float(getattr(live_spot, "volume_ratio", 0.0)),
                support_strength_score=_safe_float(getattr(live_spot, "support_strength_score", 0.0)),
                sector_strength_score=_safe_float(sector.get("strength_score")),
                sector_continuity_score=sector_continuity_score,
                memory_score=_safe_float(quiet_memory_features.get("memory_score")),
                bull_score=_safe_float(quiet_bull_row.get("total_score")),
                main_net_inflow_pct=_safe_float(getattr(live_spot, "main_net_inflow_pct", 0.0)),
                kline_confirmation_score=_safe_float(kline_context.get("confirmation_score")),
                strict_confirmation_count=_safe_int(kline_context.get("strict_confirmation_count")),
                overhead_gap_pct=_safe_float(kline_context.get("overhead_gap_pct")),
                diagnostic_route="strict_stealth_scan",
            )
            continue

        memory_features = memory_map.get(code, {})
        bull_row = _build_quiet_setup_bull_row(
            live_spot,
            sector,
            kline_context,
            existing_row=bull_map.get(code),
        )
        quiet_row = _build_quiet_setup_row(
            live_spot, bull_row, kline_context, fund_flow=fund_flow_map.get(code),
        )
        detail = quiet_row.get("detail") or {}
        change_pct = _safe_float(detail.get("change_pct"))
        if change_pct < -1.2 or change_pct >= 8.8:
            _record_first_board_diagnostic(
                blocked_diagnostics,
                code=code,
                name=str(live_spot.name or code).strip(),
                blockers=["涨幅区间不符合静默蓄势"],
                signal_summary="静默蓄势 / 多日形态确认",
                trigger_reason=str(quiet_row.get("primary_reason") or "静默蓄势扫描"),
                sector_name=str(sector.get("sector_name") or ""),
                latest_as_of=str(quiet_row.get("latest_as_of") or ""),
                change_pct=change_pct,
                volume_ratio=_safe_float(detail.get("volume_ratio")),
                support_strength_score=_safe_float(detail.get("support_strength_score")),
                sector_strength_score=_safe_float(sector.get("strength_score")),
                sector_continuity_score=sector_continuity_score,
                memory_score=_safe_float(memory_features.get("memory_score")),
                bull_score=_safe_float(bull_row.get("total_score")),
                main_net_inflow_pct=_safe_float(detail.get("main_net_inflow_pct")),
                kline_confirmation_score=_safe_float(kline_context.get("confirmation_score")),
                strict_confirmation_count=_safe_int(kline_context.get("strict_confirmation_count")),
                overhead_gap_pct=_safe_float(kline_context.get("overhead_gap_pct")),
                amplitude=_safe_float(detail.get("amplitude")),
                provisional_last_bar=bool(kline_context.get("provisional_last_bar")),
                diagnostic_route="strict_stealth_scan",
            )
            continue

        bull_score = _safe_float(bull_row.get("total_score"))
        strict_pass, strict_blockers, strict_route = _passes_strict_first_board_gate(
            quiet_row,
            bull_score=bull_score,
            sector=sector,
            kline_context=kline_context,
            memory_features=memory_features,
            market_context=market_context,
        )
        if not strict_pass:
            _record_first_board_diagnostic(
                blocked_diagnostics,
                code=code,
                name=str(live_spot.name or code).strip(),
                blockers=strict_blockers,
                signal_summary="静默蓄势 / 多日形态确认",
                trigger_reason=str(quiet_row.get("primary_reason") or "静默蓄势扫描"),
                sector_name=str(sector.get("sector_name") or ""),
                latest_as_of=str(quiet_row.get("latest_as_of") or ""),
                change_pct=change_pct,
                volume_ratio=_safe_float(detail.get("volume_ratio")),
                support_strength_score=_safe_float(detail.get("support_strength_score")),
                sector_strength_score=_safe_float(sector.get("strength_score")),
                sector_continuity_score=sector_continuity_score,
                memory_score=_safe_float(memory_features.get("memory_score")),
                bull_score=bull_score,
                main_net_inflow_pct=_safe_float(detail.get("main_net_inflow_pct")),
                kline_confirmation_score=_safe_float(kline_context.get("confirmation_score")),
                strict_confirmation_count=_safe_int(kline_context.get("strict_confirmation_count")),
                overhead_gap_pct=_safe_float(kline_context.get("overhead_gap_pct")),
                amplitude=_safe_float(detail.get("amplitude")),
                provisional_last_bar=bool(kline_context.get("provisional_last_bar")),
                diagnostic_route=strict_route or "strict_stealth_scan",
            )
            relaxed_probability, relaxed_probability_factors = _build_first_board_probability(
                quiet_row,
                bull_row,
                sector,
                market_context,
                kline_context=kline_context,
                memory_features=memory_features,
                candidate_route=_candidate_route_from_strict_route(
                    strict_route,
                    sector=sector,
                    bull_score=bull_score,
                    support_strength=_safe_float(detail.get("support_strength_score")),
                ),
            )
            adjusted_probability, relaxed_meta = _adjust_relaxed_first_board_probability(
                relaxed_probability,
                strict_blockers=strict_blockers,
                route=str(strict_route or "strict_stealth_scan"),
            )
            adjusted_sub_probabilities = {
                key: _clamp_probability(max(0.05, _safe_float(value) - _safe_float(relaxed_meta.get("sub_probability_penalty"))))
                for key, value in (relaxed_probability_factors.get("sub_probabilities") or {}).items()
            }
            adjusted_sub_probabilities["first_limitup_next_day"] = adjusted_probability
            relaxed_probability_factors = {
                **relaxed_probability_factors,
                "route_score": _clamp_score(
                    _safe_float(relaxed_probability_factors.get("route_score"))
                    - _safe_float(relaxed_meta.get("route_score_penalty"))
                ),
                "sub_probabilities": adjusted_sub_probabilities,
                "relaxed_watch_penalty": _safe_float(relaxed_meta.get("penalty")),
                "relaxed_watch_blocker_categories": relaxed_meta.get("blocker_categories") or [],
                "strict_gate_status": "watch_relaxed",
            }
            if _should_keep_relaxed_first_board_watch(
                quiet_row,
                bull_score=bull_score,
                sector=sector,
                kline_context=kline_context,
                memory_features=memory_features,
                probability=adjusted_probability,
                probability_factors=relaxed_probability_factors,
                strict_blockers=strict_blockers,
            ):
                sector_reason = str(sector.get("sector_name") or "")
                blocker_reason = str(strict_blockers[0] if strict_blockers else "未进冲刺主池")
                signal_summary, primary_reason, secondary_reason = _resolve_first_board_display_reasoning(
                    probability_factors=relaxed_probability_factors,
                    signal_summary="静默蓄势 / 多日形态确认",
                    primary_reason=str(quiet_row.get("primary_reason") or ""),
                    secondary_reason=" · ".join(
                        item
                        for item in [f"未进冲刺主池：{blocker_reason}", str(quiet_row.get("secondary_reason") or ""), sector_reason]
                        if item
                    ),
                    sector_reason=sector_reason,
                    blocker_reason=f"未进冲刺主池：{blocker_reason}",
                )
                signal_state = _resolve_promotion_signal_status(
                    target_board=1,
                    trade_date=trade_date,
                    latest_as_of=str(quiet_row.get("latest_as_of") or ""),
                    kline_context=kline_context,
                    today=_resolve_signal_clock(str(snapshot_time or quiet_row.get("latest_as_of") or ""), trade_date)[0],
                    session_name=_resolve_signal_clock(str(snapshot_time or quiet_row.get("latest_as_of") or ""), trade_date)[1],
                )
                candidates.append(
                    _build_first_board_candidate_item(
                        code=code,
                        name=str(live_spot.name or code).strip(),
                        probability=adjusted_probability,
                        probability_factors=relaxed_probability_factors,
                        setup_grade=str(quiet_row.get("setup_grade") or ""),
                        setup_grade_display="静默蓄势",
                        signal_summary=signal_summary,
                        primary_reason=primary_reason,
                        secondary_reason=secondary_reason,
                        detail=detail,
                        display_score=_safe_float(quiet_row.get("display_score")),
                        bull_row=bull_row,
                        bull_score=bull_score,
                        sector=sector,
                        sector_reason=sector_reason,
                        event_types=["stealth_setup"],
                        memory_features=memory_features,
                        kline_context=kline_context,
                        risk_flags=[],
                        strict_blockers=strict_blockers,
                        prediction_mode=strict_route or "strict_stealth_scan",
                        latest_as_of=str(quiet_row.get("latest_as_of") or ""),
                        signal_state=signal_state,
                        time_horizon="watch",
                        time_horizon_label="首板梯队",
                        time_horizon_reason="静默蓄势尚未达到冲刺标准，但仍保留在首板预测梯队中继续跟踪",
                        keep_in_diagnostics=True,
                        trade_ready=False,
                        strict_gate_passed=False,
                    )
                )
                existing_codes.add(code)
            continue

        probability, probability_factors = _build_first_board_probability(
            quiet_row,
            bull_row,
            sector,
            market_context,
            kline_context=kline_context,
            memory_features=memory_features,
            candidate_route=_candidate_route_from_strict_route(
                strict_route,
                sector=sector,
                bull_score=bull_score,
                support_strength=_safe_float(detail.get("support_strength_score")),
            ),
        )
        sector_reason = str(sector.get("sector_name") or "")
        signal_summary, primary_reason, secondary_reason = _resolve_first_board_display_reasoning(
            probability_factors=probability_factors,
            signal_summary="静默蓄势 / 多日形态确认",
            primary_reason=str(quiet_row.get("primary_reason") or ""),
            secondary_reason=" · ".join(
                item for item in [str(quiet_row.get("secondary_reason") or ""), sector_reason] if item
            ),
            sector_reason=sector_reason,
        )
        signal_state = _resolve_promotion_signal_status(
            target_board=1,
            trade_date=trade_date,
            latest_as_of=str(quiet_row.get("latest_as_of") or ""),
            kline_context=kline_context,
            today=_resolve_signal_clock(str(snapshot_time or quiet_row.get("latest_as_of") or ""), trade_date)[0],
            session_name=_resolve_signal_clock(str(snapshot_time or quiet_row.get("latest_as_of") or ""), trade_date)[1],
        )
        candidates.append(
            _build_first_board_candidate_item(
                code=code,
                name=str(live_spot.name or code).strip(),
                probability=probability,
                probability_factors=probability_factors,
                setup_grade=str(quiet_row.get("setup_grade") or ""),
                setup_grade_display="静默蓄势",
                signal_summary=signal_summary,
                primary_reason=primary_reason,
                secondary_reason=secondary_reason,
                detail=detail,
                display_score=_safe_float(quiet_row.get("display_score")),
                bull_row=bull_row,
                bull_score=bull_score,
                sector=sector,
                sector_reason=sector_reason,
                event_types=["stealth_setup"],
                memory_features=memory_features,
                kline_context=kline_context,
                risk_flags=[],
                strict_blockers=strict_blockers,
                prediction_mode=strict_route or "strict_stealth_scan",
                latest_as_of=str(quiet_row.get("latest_as_of") or ""),
                signal_state=signal_state,
            )
        )
        existing_codes.add(code)

    candidates = _dedupe_first_board_candidates(await stock_tagger.filter_signals(db, candidates))
    accepted_codes = {
        str(item.get("code") or "").strip()
        for item in candidates
        if str(item.get("code") or "").strip() and not bool(item.get("keep_in_diagnostics"))
    }
    for accepted_code in accepted_codes:
        blocked_diagnostics.pop(accepted_code, None)
    candidates.sort(key=_first_board_candidate_pool_priority, reverse=True)
    return (
        candidates[:limit],
        snapshot.get("snapshot_time"),
        _build_first_board_diagnostics_payload(blocked_diagnostics),
    )


async def _build_second_board_candidates(
    db: AsyncSession,
    trade_date: date,
    limit_ups: list[dict],
    market_context: dict,
    limit: int,
    *,
    include_live_dragon_tiger: bool = True,
) -> list[dict]:
    sentiment_cycle = await _load_sentiment_cycle(db, trade_date)
    first_boards = [item for item in limit_ups if _safe_int(item.get("consecutive_days"), 1) == 1]
    codes = [str(item.get("code") or "") for item in first_boards if item.get("code")]
    reason_by_code = {
        str(item.get("code") or ""): str(item.get("limit_up_reason") or "")
        for item in first_boards
    }
    sector_map = await _load_best_sector_context(
        db,
        trade_date,
        codes,
        reason_by_code=reason_by_code,
    )
    spot_map = await _load_spot_map(db, codes)
    fund_flow_map = await _load_fund_flow_map(db, codes, trade_date)
    kline_shape_map = await _load_second_board_kline_shape_map(db, codes, trade_date)
    position_map = await _load_second_board_position_map(db, codes, trade_date)
    reason_context_map = _build_second_board_reason_context(first_boards, spot_map)
    morning_sector_context_map = _build_second_board_morning_sector_context(first_boards, sector_map)
    dragon_tiger_map = (
        await _load_dragon_tiger_context_map(trade_date, codes)
        if include_live_dragon_tiger
        else _load_cached_dragon_tiger_context_map(trade_date, codes)
    )

    candidates: list[dict] = []
    for item in first_boards:
        code = str(item.get("code") or "").strip()
        if not code:
            continue
        sector = sector_map.get(code, {})
        spot = spot_map.get(code)
        fund_flow = fund_flow_map.get(code)
        reason_context = reason_context_map.get(code, {})
        morning_sector_context = morning_sector_context_map.get(code, {})
        dragon_tiger = dragon_tiger_map.get(_normalize_dragon_tiger_code(code)) or _dragon_tiger_empty_context()
        prediction = tracker.predict_promotion(
            code,
            str(item.get("name") or ""),
            current_days=1,
            stock_data={
                "seal_amount": _safe_float(item.get("seal_amount")),
                "break_count": _safe_int(item.get("break_count")),
                "turnover": _safe_float(item.get("turnover")),
                "sector_strength": min(_safe_float(sector.get("strength_score")), 100) / 100,
                "sentiment_cycle": sentiment_cycle,
            },
        )
        adjusted_probability, probability_factors = _adjust_second_board_probability(
            prediction.promotion_prob,
            item=item,
            sector=sector,
            market_context=market_context,
            spot=spot,
            fund_flow=fund_flow,
            dragon_tiger=dragon_tiger,
            reason_context=reason_context,
            kline_shape=kline_shape_map.get(code, {}),
            morning_sector_context=morning_sector_context,
        )
        position_context = position_map.get(code, {})
        probability_factors = {**probability_factors, **position_context}
        relay_gate = _build_second_board_relay_gate(
            item=item,
            factors=probability_factors,
            sector=sector,
            position=position_context,
        )
        probability_factors.update(relay_gate)
        confidence_level = prediction.confidence
        confidence_level, confidence_score, confidence_label = _confidence_from_probability(adjusted_probability)
        signal_state = _resolve_promotion_signal_status(
            target_board=2,
            trade_date=trade_date,
            latest_as_of=str(item.get("trade_date") or ""),
        )
        dragon_tiger_summary = str(dragon_tiger.get("summary") or "")
        primary_reason = (
            f"首板封单{_safe_float(item.get('seal_amount')) / 1e8:.1f}亿，"
            f"炸板{_safe_int(item.get('break_count'))}次，换手{_safe_float(item.get('turnover')):.1f}%"
        )
        if dragon_tiger.get("is_listed") and dragon_tiger_summary:
            primary_reason = f"{primary_reason}；{dragon_tiger_summary}"
        secondary_parts = [
            (
                f"{sector.get('sector_name') or '无明显主线'} · 强度{round(_safe_float(sector.get('strength_score')))}"
                if sector
                else "以封板质量和市场情绪为主"
            )
        ]
        if probability_factors.get("market_risk_level") in {"hostile", "weak"}:
            secondary_parts.append(
                f"接力环境{probability_factors.get('market_risk_level')}，概率已降档"
            )
        if _safe_int(reason_context.get("reason_cohort_count")) >= 3:
            secondary_parts.append(
                f"{reason_context.get('reason')}首板集群{reason_context.get('reason_cohort_count')}只"
            )
        elif bool(reason_context.get("reason_strong_cluster")):
            secondary_parts.append(
                f"{reason_context.get('reason')}小集群强封"
            )
        if bool(probability_factors.get("sector_morning_strengthening")):
            secondary_parts.append(
                f"{probability_factors.get('sector_morning_sector_name') or sector.get('sector_name') or '同板块'}早盘强化，"
                f"首板{_safe_int(probability_factors.get('sector_morning_first_board_count'))}只，"
                f"早封{_safe_int(probability_factors.get('sector_morning_early_count'))}只"
            )
        if bool(probability_factors.get("sector_low_position_rotation")):
            secondary_parts.append(
                f"{probability_factors.get('sector_rotation_label') or '低位轮动'}，强度变化{_safe_float(probability_factors.get('sector_strength_delta')):+.1f}"
            )
        elif bool(probability_factors.get("sector_crowded_stale_theme")):
            secondary_parts.append("旧主线退潮，已做降权")
        if dragon_tiger.get("is_listed"):
            probability_delta = _safe_float(dragon_tiger.get("probability_delta"))
            secondary_parts.append(
                f"龙虎榜席位修正{probability_delta * 100:+.1f}pct"
            )
        if probability_factors.get("second_board_route") in {"low_rotation_second_board", "cluster_second_board", "low_rotation_cluster_second_board"}:
            secondary_parts.append(str(probability_factors.get("second_board_route_label") or "低位轮动/集群二板"))
        candidates.append(
            {
                "code": code,
                "name": str(item.get("name") or "").strip(),
                "target_board": 2,
                "target_label": "冲二板",
                "probability": adjusted_probability,
                "main_probability_name": SECOND_BOARD_MAIN_PROBABILITY_NAME,
                "sub_probabilities": {"next_second_board": adjusted_probability},
                "confidence": confidence_score,
                "confidence_level": confidence_level,
                "confidence_label": confidence_label,
                "setup_grade": "首板晋级",
                "setup_grade_display": "首板晋级",
                "signal_summary": "首板→二板",
                "primary_reason": primary_reason,
                "secondary_reason": " · ".join(item for item in secondary_parts if item),
                "current_price": _safe_float(getattr(spot, "price", 0)) or _safe_float(item.get("limit_up_price")),
                "change_pct": _safe_float(getattr(spot, "change_pct", 0)),
                "turnover": _safe_float(item.get("turnover")),
                "volume_ratio": _safe_float(getattr(spot, "volume_ratio", 0)),
                "main_net_inflow": _safe_float(probability_factors.get("main_net_inflow")),
                "main_net_inflow_pct": _safe_float(probability_factors.get("main_net_inflow_pct")),
                "support_strength_score": _safe_float(getattr(spot, "support_strength_score", 0)),
                "bull_score": 0.0,
                "bull_level": "",
                "display_score": adjusted_probability * 100,
                "event_types": ["limit_up"],
                "sector_name": str(sector.get("sector_name") or ""),
                "sector_strength_score": _safe_float(sector.get("strength_score")),
                "sector_limit_up_count": _safe_int(sector.get("limit_up_count")),
                "sector_consecutive_days": _safe_int(sector.get("consecutive_days")),
                "candidate_route": "second_board_promotion",
                "candidate_route_label": FIRST_BOARD_ROUTE_LABELS["second_board_promotion"],
                "second_board_route": str(probability_factors.get("second_board_route") or "standard_second_board"),
                "second_board_route_label": str(probability_factors.get("second_board_route_label") or "常规二板晋级"),
                "route_score": round(adjusted_probability * 100, 2),
                "probability_factors": probability_factors,
                "dragon_tiger": dragon_tiger,
                "dragon_tiger_score": _safe_float(dragon_tiger.get("member_score"), 50.0),
                "dragon_tiger_probability_delta": _safe_float(dragon_tiger.get("probability_delta")),
                "second_board_style_score": _safe_float(probability_factors.get("second_board_style_score")),
                "relay_pool_ready": bool(relay_gate.get("relay_pool_ready")),
                "relay_quality_score": _safe_float(relay_gate.get("relay_quality_score")),
                "relay_blockers": list(relay_gate.get("relay_blockers") or []),
                "relay_prediction_ready": bool(relay_gate.get("relay_prediction_ready")),
                "relay_prediction_quality_score": _safe_float(relay_gate.get("relay_prediction_quality_score")),
                "relay_prediction_only": bool(relay_gate.get("relay_prediction_only")),
                "relay_prediction_blockers": list(relay_gate.get("relay_prediction_blockers") or []),
                "trade_ready": bool(relay_gate.get("relay_pool_ready")),
                "relay_action_label": (
                    "可交易监控"
                    if bool(relay_gate.get("relay_pool_ready"))
                    else "只预测·不可追"
                    if bool(relay_gate.get("relay_prediction_ready"))
                    else "未入正式预测"
                ),
                "learning_bucket": str(relay_gate.get("relay_learning_bucket") or ""),
                "seal_amount": _safe_float(item.get("seal_amount")),
                "break_count": _safe_int(item.get("break_count")),
                "limit_up_time": str(item.get("limit_up_time") or ""),
                "limit_up_reason": str(item.get("limit_up_reason") or ""),
                "latest_as_of": str(item.get("trade_date") or ""),
                **signal_state,
            }
        )

    pre_filter_count = len(candidates)
    candidates = await stock_tagger.filter_signals(db, candidates)
    post_filter_count = len(candidates)
    for item in candidates:
        item["probability_factors"] = {
            **(item.get("probability_factors") or {}),
            "second_board_prefilter_pool_size": pre_filter_count,
            "second_board_postfilter_pool_size": post_filter_count,
            "second_board_filter_removed_count": max(pre_filter_count - post_filter_count, 0),
        }
    candidates.sort(
        key=lambda item: (
            _safe_float(item.get("probability")),
            _safe_float(item.get("second_board_style_score")),
            _safe_float(item.get("sector_strength_score")),
            _safe_float(item.get("dragon_tiger_score")),
            -_safe_float(item.get("turnover")),
            -_safe_int(item.get("break_count")),
        ),
        reverse=True,
    )
    return candidates[:limit]


def _merge_ranked_candidates(first_board: list[dict], second_board: list[dict], limit: int) -> list[dict]:
    merged = list(first_board) + list(second_board)
    merged.sort(
        key=lambda item: (
            _safe_float(item.get("probability")),
            1 if _safe_int(item.get("target_board")) == 2 else 0,
            SETUP_GRADE_RANK.get(str(item.get("setup_grade") or ""), -1),
            _safe_float(item.get("bull_score")),
            _safe_float(item.get("display_score")),
        ),
        reverse=True,
    )
    return merged[:limit]


@router.get("/ladder")
async def promotion_ladder(db: AsyncSession = Depends(get_db)):
    """连板梯队(已过滤停牌/退市/ST)"""
    trade_date = await _get_latest_limit_up_trade_date(db)
    limit_ups = await _load_filtered_limit_ups(db, trade_date)
    ladders = tracker.build_ladder(limit_ups)

    return {
        "trade_date": str(trade_date),
        "ladder": [
            {
                "consecutive_days": ladder.consecutive_days,
                "count": ladder.count,
                "seal_rate": ladder.seal_rate,
                "stocks": [
                    {
                        "code": stock.get("code", ""),
                        "name": stock.get("name", ""),
                        "seal_amount": stock.get("seal_amount", 0),
                        "tag": stock.get("tag"),
                        "is_tradeable": stock.get("is_tradeable", True),
                    }
                    for stock in ladder.stocks[:5]
                ],
            }
            for ladder in ladders
        ],
    }


@router.get("/board-height")
async def board_height(db: AsyncSession = Depends(get_db)):
    """市场连板高度"""
    trade_date = await _get_latest_limit_up_trade_date(db)
    limit_ups = await _load_filtered_limit_ups(db, trade_date)
    active_limit_ups = list(limit_ups)
    seal_rate_method = "historical_pool"
    if trade_date == date.today() and limit_ups:
        spot_map = await _load_spot_map(
            db,
            [str(item.get("code") or "").strip() for item in limit_ups],
        )
        live_rows = []
        for item in limit_ups:
            spot = spot_map.get(str(item.get("code") or "").strip())
            current_price = _safe_float(getattr(spot, "price", 0))
            limit_up_price = _safe_float(getattr(spot, "limit_up", 0)) or _safe_float(item.get("limit_up_price"))
            if current_price > 0 and limit_up_price > 0 and current_price >= limit_up_price - 0.005:
                live_rows.append(item)
        if spot_map:
            active_limit_ups = live_rows
            seal_rate_method = "live_price_at_limit"

    board = tracker.get_board_height(active_limit_ups)

    previous_trade_date = await _get_previous_limit_up_trade_date(db, trade_date)
    previous_limit_ups = await _load_filtered_limit_ups(db, previous_trade_date)
    previous_codes = {item["code"] for item in previous_limit_ups}
    current_codes = {item["code"] for item in active_limit_ups}
    promoted_count = len(previous_codes & current_codes) if previous_codes else 0

    touched_limit_up_count = len(limit_ups)
    limit_up_count = len(active_limit_ups)
    zero_break_count = sum(1 for item in limit_ups if int(item.get("break_count") or 0) == 0)
    if seal_rate_method == "live_price_at_limit":
        seal_rate = round(limit_up_count / touched_limit_up_count * 100, 1) if touched_limit_up_count else 0
    else:
        # 历史池没有可靠的实时现价，沿用收盘快照的零炸板口径。
        # 这与盘中“当前仍封住/曾触板”的口径明确分开。
        seal_rate = round(zero_break_count / touched_limit_up_count * 100, 1) if touched_limit_up_count else 0
    promotion_rate = round(promoted_count / len(previous_codes), 4) if previous_codes else 0

    return {
        **board,
        "trade_date": str(trade_date),
        "previous_trade_date": str(previous_trade_date) if previous_trade_date else None,
        "limit_up_count": limit_up_count,
        "touched_limit_up_count": touched_limit_up_count,
        "seal_rate": seal_rate,
        "seal_rate_method": seal_rate_method,
        "zero_break_count": zero_break_count,
        "promotion_rate": promotion_rate,
        "promoted_count": promoted_count,
    }


@router.get(
    "/candidates",
    summary="晋级候选池",
    description=(
        "返回正式展示用的首板候选、板后贴板横盘预备池、贴板池未入池诊断、首板预测梯队分层、二板候选，以及分赛道概率榜。"
        " `ranked_first_board_candidates` 是正式 Top12，`ranked_first_board_recall_candidates` 是 Top30 宽召回观察层，`ranked_second_board_candidates` 是二板主口径；"
        " `first_board_diagnostics` 会解释首板票为什么没进主筛选，`limit_up_platform_diagnostics` 会解释近期涨停股为什么还没进贴板横盘预备池。"
        " 混合排序只保留在 `debug.mixed_ranked_candidates` 中供联调排查使用，不建议前端主展示或业务决策直接使用。"
    ),
)
async def promotion_candidates(
    limit: int = 12,
    ranked_limit: int = 10,
    force_refresh: bool = False,
    compact: bool = False,
    db: AsyncSession = Depends(get_db),
):
    """Public page reads can never mint an official schedule snapshot."""

    return await build_promotion_candidates(
        limit=limit,
        ranked_limit=ranked_limit,
        snapshot_source="page",
        snapshot_context="page",
        force_refresh=force_refresh,
        compact=compact,
        db=db,
    )


async def build_promotion_candidates(
    limit: int = 12,
    ranked_limit: int = 10,
    snapshot_source: str = "page",
    snapshot_context: str = "",
    force_refresh: bool = False,
    compact: bool = False,
    db: AsyncSession | None = None,
    quality_gate: dict | None = None,
):
    """Internal builder; only the scheduler may request append-only official runs."""

    if db is None:
        raise ValueError("database session is required")
    request_started_at = datetime.now()
    candidate_limit = max(5, min(limit, 30))
    ranked_candidate_limit = max(candidate_limit, min(ranked_limit, 50))
    formal_ranked_limit = min(
        ranked_candidate_limit,
        PROMOTION_MAX_FORMAL_RANKED,
    )
    recall_ranked_limit = min(
        ranked_candidate_limit,
        PROMOTION_MAX_RECALL_RANKED,
    )
    canonical_candidate_limit = max(candidate_limit, PROMOTION_PAGE_CACHE_MIN_CANDIDATE_CAPACITY)
    canonical_formal_ranked_limit = PROMOTION_MAX_FORMAL_RANKED
    canonical_recall_ranked_limit = PROMOTION_MAX_RECALL_RANKED
    normalized_snapshot_source = _normalize_prediction_snapshot_source(snapshot_source)
    is_schedule_snapshot = normalized_snapshot_source == "schedule"
    normalized_snapshot_context = _normalize_prediction_snapshot_context(
        snapshot_context,
        source=normalized_snapshot_source,
    )
    # Research material is locked once, before candidate work. Page/intraday
    # requests do no archive IO. Default unreviewed sources stay explicitly blocked.
    from app.config.settings import settings
    daily_hist_context = await prepare_daily_hist_context(
        snapshot_source=normalized_snapshot_source,
        snapshot_context=normalized_snapshot_context,
        request_started_at=request_started_at,
        archive_root=settings.PROMOTION_DAILY_MATERIAL_DIR,
        timeout_sec=settings.PROMOTION_DAILY_MATERIAL_READ_TIMEOUT_SEC,
    )
    page_cache_key = _promotion_page_cache_key(db, candidate_limit, ranked_candidate_limit, compact)
    cache_scope = _promotion_cache_scope(db)
    if not is_schedule_snapshot and not force_refresh:
        cached_payload = _PROMOTION_PAGE_CANDIDATES_CACHE.get(page_cache_key)
        now = time.monotonic()
        if cached_payload and now - cached_payload[0] <= PROMOTION_PAGE_CACHE_TTL_SECONDS:
            payload = dict(cached_payload[1])
            prediction_health = dict(payload.get("prediction_health") or {})
            prediction_health["page_cache_hit"] = True
            prediction_health["page_cache_age_seconds"] = round(now - cached_payload[0], 3)
            payload["prediction_health"] = prediction_health
            return payload
        latest_payload = _PROMOTION_LATEST_CANDIDATES_CACHE.get(cache_scope)
        if (
            latest_payload
            and now - latest_payload[0] <= PROMOTION_SCHEDULE_CACHE_MAX_AGE_SECONDS
            and _promotion_cached_payload_supports_request(
                latest_payload[1],
                candidate_limit=candidate_limit,
                ranked_limit=recall_ranked_limit,
            )
        ):
            payload = dict(latest_payload[1])
            prediction_health = dict(payload.get("prediction_health") or {})
            prediction_health["page_cache_hit"] = True
            prediction_health["page_cache_source"] = "latest_completed_snapshot"
            prediction_health["page_cache_age_seconds"] = round(now - latest_payload[0], 3)
            payload["prediction_health"] = prediction_health
            return _project_promotion_candidates_payload(
                payload,
                compact=compact,
                candidate_limit=candidate_limit,
                ranked_limit=recall_ranked_limit,
            )
        persisted_trade_date = await _get_latest_limit_up_trade_date(db)
        persisted_payload = None
        persisted_source = ""
        for snapshot_key, source_label in (
            (PROMOTION_CANDIDATES_SNAPSHOT_KEY, "persisted_schedule_snapshot"),
            (PROMOTION_PAGE_CANDIDATES_SNAPSHOT_KEY, "persisted_page_snapshot"),
        ):
            candidate_payload = await _get_latest_persisted_dashboard_snapshot(
                db,
                snapshot_key,
                persisted_trade_date,
            )
            if (
                candidate_payload
                and candidate_payload.get("prediction_model_version") == PROMOTION_MODEL_VERSION
                and _promotion_cached_payload_supports_request(
                    candidate_payload,
                    candidate_limit=candidate_limit,
                    ranked_limit=recall_ranked_limit,
                )
            ):
                persisted_payload = candidate_payload
                persisted_source = source_label
                break
        if persisted_payload:
            payload = dict(persisted_payload)
            prediction_health = dict(payload.get("prediction_health") or {})
            prediction_health["page_cache_hit"] = True
            prediction_health["page_cache_source"] = persisted_source
            payload["prediction_health"] = prediction_health
            cached_at = time.monotonic()
            _PROMOTION_LATEST_CANDIDATES_CACHE[cache_scope] = (cached_at, payload)
            projected_payload = _project_promotion_candidates_payload(
                payload,
                compact=compact,
                candidate_limit=candidate_limit,
                ranked_limit=recall_ranked_limit,
            )
            _PROMOTION_PAGE_CANDIDATES_CACHE[page_cache_key] = (cached_at, projected_payload)
            return projected_payload
    internal_candidate_limit = (
        max(ranked_candidate_limit, PROMOTION_INTERNAL_RECORD_LIMIT)
        if is_schedule_snapshot
        else max(ranked_candidate_limit, min(PROMOTION_INTERNAL_RECORD_LIMIT, 120))
    )

    first_board_snapshot = await prewarm_anomaly_snapshot(db, trade_date=None)
    first_board_trade_date = await _resolve_first_board_trade_date(db, first_board_snapshot)
    first_board_limit_ups = await _load_filtered_limit_ups(db, first_board_trade_date)
    first_board_limit_up_codes = {str(item.get("code") or "") for item in first_board_limit_ups}
    first_board_market_context = await _enrich_market_ladder_context(
        db,
        first_board_trade_date,
        _build_market_ladder_context(first_board_limit_ups),
    )

    second_board_trade_date = await _resolve_second_board_source_trade_date(
        db,
        snapshot_source=normalized_snapshot_source,
        snapshot_context=normalized_snapshot_context,
        recorded_at=request_started_at,
    )
    limit_ups = await _load_filtered_limit_ups(db, second_board_trade_date)
    second_board_market_context = await _enrich_market_ladder_context(
        db,
        second_board_trade_date,
        _build_market_ladder_context(limit_ups),
    )
    frozen_regime_by_date: dict[date, dict] = {}
    regime_freeze_errors: list[dict] = []
    if is_schedule_snapshot:
        if quality_gate is None:
            raise ValueError("official schedule snapshots require an explicit data-quality gate")
        await _validate_official_snapshot_clock(
            normalized_snapshot_context,
            request_started_at,
            {first_board_trade_date, second_board_trade_date},
        )
    if (
        is_schedule_snapshot
        and normalized_snapshot_context in PROMOTION_CANONICAL_CLOSE_CONTEXTS
    ):
        for regime_trade_date in sorted(
            {first_board_trade_date, second_board_trade_date}
        ):
            try:
                frozen_regime_by_date[regime_trade_date] = (
                    await build_market_regime_snapshot(
                        db,
                        trade_date=regime_trade_date,
                        snapshot_context="postmarket",
                        as_of_at=request_started_at,
                        persist=True,
                    )
                )
            except Exception as regime_exc:
                # Regime enrichment may degrade to unknown, but must never erase a
                # valid legacy prediction. The failure remains visible in payload.
                regime_freeze_errors.append(
                    {
                        "trade_date": str(regime_trade_date),
                        "error": str(regime_exc),
                    }
                )
    evaluated_learning_records = await _refresh_promotion_learning(db) if is_schedule_snapshot else 0
    learning_stats = await _load_promotion_learning_stats(db)
    prediction_news_end_time = await _resolve_promotion_snapshot_news_end_time(
        first_board_trade_date,
        snapshot_source=normalized_snapshot_source,
        snapshot_context=snapshot_context,
        now=request_started_at,
    )

    first_board_all_candidates, snapshot_time, first_board_diagnostics = await _build_first_board_candidates(
        db,
        first_board_trade_date,
        first_board_limit_up_codes,
        first_board_market_context,
        internal_candidate_limit,
        snapshot=first_board_snapshot,
        news_end_time=prediction_news_end_time,
    )
    first_board_all_candidates = _attach_frozen_market_regime(
        first_board_all_candidates,
        frozen_regime_by_date.get(first_board_trade_date),
    )
    first_board_all_candidates = _apply_promotion_learning_to_candidates(first_board_all_candidates, learning_stats)
    first_board_deployment = {"applied": False, "reason": "page_read_only"}
    if is_schedule_snapshot:
        first_board_all_candidates, first_board_deployment = await apply_active_promotion_overlay(
            db,
            first_board_all_candidates,
            target_board=1,
            trade_date=first_board_trade_date,
            snapshot_context=normalized_snapshot_context,
        )
    first_board_candidates, first_board_pre_sprint_candidates, first_board_watch_candidates, first_board_weak_watch_candidates = _split_first_board_candidates(first_board_all_candidates)
    first_board_pre_sprint_groups = _build_first_board_watch_groups(
        first_board_pre_sprint_candidates[:canonical_candidate_limit],
        limit_per_group=canonical_candidate_limit,
        time_horizon="pre_sprint",
    )
    first_board_watch_groups = _build_first_board_watch_groups(
        first_board_watch_candidates[:canonical_candidate_limit],
        limit_per_group=canonical_candidate_limit,
    )
    first_board_weak_watch_groups = _build_first_board_watch_groups(
        first_board_weak_watch_candidates[:canonical_candidate_limit],
        limit_per_group=canonical_candidate_limit,
        time_horizon="weak_watch",
    )
    limit_up_platform_pool = _build_limit_up_platform_pool(
        first_board_all_candidates,
        limit=canonical_candidate_limit,
    )
    limit_up_platform_diagnostics = _build_limit_up_platform_diagnostics_payload(
        first_board_all_candidates,
        limit_up_platform_pool["selected"],
        limit_per_group=canonical_candidate_limit,
    )
    ranked_first_board_candidates = _rank_first_board_candidates(
        first_board_all_candidates,
        canonical_formal_ranked_limit,
    )
    ranked_first_board_recall_candidates = (
        _rank_first_board_recall_candidates(
            first_board_all_candidates,
            ranked_first_board_candidates,
            canonical_recall_ranked_limit,
        )
    )
    first_board_rank_eligible_candidates = _rank_first_board_candidates(
        first_board_all_candidates,
        len(first_board_all_candidates),
    )
    second_board_all_candidates = await _build_second_board_candidates(
        db,
        second_board_trade_date,
        limit_ups,
        second_board_market_context,
        internal_candidate_limit,
        include_live_dragon_tiger=is_schedule_snapshot,
    )
    second_board_all_candidates = _attach_frozen_market_regime(
        second_board_all_candidates,
        frozen_regime_by_date.get(second_board_trade_date),
    )
    second_board_all_candidates = _apply_promotion_learning_to_candidates(second_board_all_candidates, learning_stats)
    second_board_deployment = {"applied": False, "reason": "page_read_only"}
    if is_schedule_snapshot:
        second_board_all_candidates, second_board_deployment = await apply_active_promotion_overlay(
            db,
            second_board_all_candidates,
            target_board=2,
            trade_date=second_board_trade_date,
            snapshot_context=normalized_snapshot_context,
        )
    ranked_second_board_candidates = _rank_second_board_candidates(
        second_board_all_candidates,
        canonical_formal_ranked_limit,
    )
    second_board_rank_eligible_candidates = _rank_second_board_candidates(
        second_board_all_candidates,
        len(second_board_all_candidates),
    )
    second_board_candidates = ranked_second_board_candidates[:canonical_candidate_limit]

    mixed_ranked_candidates = _merge_ranked_candidates(
        ranked_first_board_candidates,
        ranked_second_board_candidates,
        max(ranked_candidate_limit, canonical_formal_ranked_limit),
    )
    prediction_health = await _build_prediction_snapshot_health(
        db,
        first_board_trade_date=first_board_trade_date,
        second_board_trade_date=second_board_trade_date,
        actual_trade_date=second_board_trade_date,
    )
    recorded_predictions = 0
    prediction_run_id = None
    recordable_first_board_candidates = [
        item for item in first_board_all_candidates if _is_recordable_prediction_candidate(item)
    ]
    annotated_first_board_records = _annotate_prediction_record_metadata(
        recordable_first_board_candidates,
        ranked_first_board_candidates,
        ranked_limit=canonical_formal_ranked_limit,
        recall_ranked_candidates=ranked_first_board_recall_candidates,
        recall_ranked_limit=canonical_recall_ranked_limit,
        rank_eligible_candidates=first_board_rank_eligible_candidates,
        snapshot_source=normalized_snapshot_source,
        snapshot_context=snapshot_context,
        news_end_time=prediction_news_end_time,
        recorded_at=request_started_at,
        candidate_anchor_trade_date=first_board_trade_date,
    )
    direction_research = _direction_research_payload(annotated_first_board_records)
    if is_schedule_snapshot:
        prediction_record_candidates = annotated_first_board_records + _annotate_prediction_record_metadata(
            second_board_all_candidates,
            ranked_second_board_candidates,
            ranked_limit=canonical_formal_ranked_limit,
            recall_ranked_candidates=ranked_second_board_candidates,
            recall_ranked_limit=canonical_formal_ranked_limit,
            rank_eligible_candidates=second_board_rank_eligible_candidates,
            snapshot_source=normalized_snapshot_source,
            snapshot_context=snapshot_context,
            news_end_time=prediction_news_end_time,
            recorded_at=request_started_at,
            candidate_anchor_trade_date=second_board_trade_date,
        )
        prediction_trade_dates = _prediction_trade_dates_by_target(
            first_board_source_trade_date=first_board_trade_date,
            second_board_source_trade_date=second_board_trade_date,
            snapshot_source=normalized_snapshot_source,
            snapshot_context=normalized_snapshot_context,
            recorded_at=request_started_at,
        )
        # Enrich NEW immutable records only, after all Champion scoring/ranking.
        # No existing candidate/score/order gate is recomputed from research data.
        prediction_record_candidates, daily_hist_summary = await attach_daily_hist_evidence(
            prediction_record_candidates, daily_hist_context,
            prediction_trade_dates=prediction_trade_dates,
        )
        persistence_result = await _record_promotion_predictions(
            db,
            prediction_record_candidates,
            prediction_trade_dates,
            snapshot_source=normalized_snapshot_source,
            quality_gate=quality_gate,
            return_details=True,
            schedule_batch=ScheduleBatch(normalized_snapshot_context, request_started_at),
        )
        recorded_predictions = persistence_result.touched
        prediction_run_id = (
            persistence_result.ledger.run_id
            if persistence_result.ledger is not None
            else None
        )
    prediction_health["recorded_during_request"] = recorded_predictions
    if evaluated_learning_records or recorded_predictions or prediction_run_id or frozen_regime_by_date:
        # A canonical schedule run must persist its exact point-in-time regime
        # even when prediction rows are idempotent and therefore add no new rows.
        await db.commit()
    if is_schedule_snapshot and recorded_predictions:
        prediction_health = await _build_prediction_snapshot_health(
            db,
            first_board_trade_date=first_board_trade_date,
            second_board_trade_date=second_board_trade_date,
            actual_trade_date=second_board_trade_date,
        )
        prediction_health["recorded_during_request"] = recorded_predictions

    actual_limit_up_replay = await _build_actual_limit_up_replay(
        db,
        actual_trade_date=second_board_trade_date,
        actual_limit_ups=limit_ups,
    )

    if is_schedule_snapshot:
        # Empty/filter-only attempts freeze blocking runs, never proof of a
        # complete zero-candidate universe or a successful prediction batch.
        prediction_health["persistence"] = persistence_result.as_payload()
    if is_schedule_snapshot and daily_hist_context is not None:
        prediction_health["daily_hist_research"] = daily_hist_summary
    prediction_health["page_cache_hit"] = False
    prediction_health["page_cache_ttl_seconds"] = PROMOTION_PAGE_CACHE_TTL_SECONDS
    payload = {
        "prediction_model_version": PROMOTION_MODEL_VERSION,
        "prediction_calibration_version": PROMOTION_CALIBRATION_VERSION,
        "direction_research": direction_research,
        "first_board_calibration_version": (
            PROMOTION_FIRST_BOARD_TEMPORAL_CALIBRATION_VERSION
        ),
        "prediction_snapshot_source": normalized_snapshot_source,
        "prediction_snapshot_context": normalized_snapshot_context,
        "quality_gate": quality_gate if is_schedule_snapshot else None,
        "prediction_news_end_time": (
            prediction_news_end_time.isoformat(timespec="seconds")
            if isinstance(prediction_news_end_time, datetime)
            else None
        ),
        "model_runtime": {
            **PROMOTION_MODEL_IDENTITY.as_payload(),
            "deployment_overlays": {
                "1": first_board_deployment,
                "2": second_board_deployment,
            },
            "regime_freezes": {
                str(trade_day): {
                    "id": item.get("id"),
                    "primary_regime": item.get("primary_regime"),
                    "as_of_at": item.get("as_of_at"),
                    "regime_version": item.get("regime_version"),
                    "data_version": item.get("data_version"),
                    "quality_status": item.get("quality_status"),
                }
                for trade_day, item in frozen_regime_by_date.items()
            },
            "regime_freeze_errors": regime_freeze_errors,
            "automatic_promotion": False,
        },
        "feature_version": PROMOTION_MODEL_IDENTITY.feature_version,
        "data_version": PROMOTION_MODEL_IDENTITY.data_version,
        "prediction_semantics": {
            "main_probability": "next_day_limit_up_event",
            "promotion_event_label_version": PROMOTION_LABEL_VERSION,
            "first_board_probability_calibration": (
                PROMOTION_FIRST_BOARD_TEMPORAL_CALIBRATION_VERSION
            ),
            "direction_probability": "next_day_close_above_previous_close",
            "trade_actionability": "independent_execution_gate",
            "launch_precursor_features": "T-1_kline_repair+3d_funding+primary_industry_ignition+direct_news",
            "launch_feature_version": LAUNCH_PRECURSOR_FEATURE_VERSION,
            "launch_probability_target_calibration": "limit_up_rank/direction/strong_rise_use_separate_holdout_lifts",
            "news_cutoff_policy": "immutable_content_and_analysis_available_by_cutoff; verified_direct_entity_only",
            "news_evidence_gate": {
                "contract": "news_direct_evidence_v1",
                "legacy_news_eligible": False,
                "sector_inference_status": "blocked",
                "sector_inference_reason": "sector_context_unversioned",
                "note": "板块强度/成员/行情缺少完整时点版本，暂停新闻反推成分股；不代表没有板块新闻。",
            },
            "formal_list_may_abstain": True,
            "formal_first_board_scope": "global_time_visible_evidence_top12",
            "broad_recall_scope": "formal_top12_prefix_plus_temporal_probability_to_top30",
            "probability_is_trade_actionability": False,
        },
        "_cache_capacity": {
            "candidate_limit": canonical_candidate_limit,
            "ranked_limit": canonical_recall_ranked_limit,
        },
        "trade_date": str(first_board_trade_date),
        "formal_first_board_limit": canonical_formal_ranked_limit,
        "recall_first_board_limit": canonical_recall_ranked_limit,
        "first_board_trade_date": str(first_board_trade_date),
        "second_board_trade_date": str(second_board_trade_date),
        "snapshot_time": snapshot_time,
        "market_context": {
            "first_board": first_board_market_context,
            "second_board": second_board_market_context,
        },
        "first_board_candidates": first_board_candidates[:canonical_candidate_limit],
        "first_board_pre_sprint_candidates": first_board_pre_sprint_candidates[:canonical_candidate_limit],
        "first_board_watch_candidates": first_board_watch_candidates[:canonical_candidate_limit],
        "first_board_weak_watch_candidates": first_board_weak_watch_candidates[:canonical_candidate_limit],
        "first_board_pre_sprint_overview": first_board_pre_sprint_groups["overview"],
        "first_board_pre_sprint_groups": first_board_pre_sprint_groups["groups"],
        "first_board_watch_overview": first_board_watch_groups["overview"],
        "first_board_watch_groups": first_board_watch_groups["groups"],
        "first_board_weak_watch_overview": first_board_weak_watch_groups["overview"],
        "first_board_weak_watch_groups": first_board_weak_watch_groups["groups"],
        "limit_up_platform_candidates": limit_up_platform_pool["candidates"],
        "limit_up_platform_overview": limit_up_platform_pool["overview"],
        "limit_up_platform_diagnostics": limit_up_platform_diagnostics,
        "first_board_diagnostics": first_board_diagnostics,
        "second_board_candidates": second_board_candidates,
        "ranked_first_board_candidates": ranked_first_board_candidates,
        "ranked_first_board_recall_candidates": (
            ranked_first_board_recall_candidates
        ),
        "ranked_second_board_candidates": ranked_second_board_candidates,
        "prediction_health": prediction_health,
        "actual_limit_up_replay": actual_limit_up_replay,
        "first_board_count": len(first_board_candidates[:canonical_candidate_limit]),
        "first_board_pre_sprint_count": len(first_board_pre_sprint_candidates[:canonical_candidate_limit]),
        "first_board_watch_count": len(first_board_watch_candidates[:canonical_candidate_limit]),
        "first_board_weak_watch_count": len(first_board_weak_watch_candidates[:canonical_candidate_limit]),
        "limit_up_platform_count": len(limit_up_platform_pool["candidates"]),
        "second_board_count": len(second_board_candidates),
        "ranked_first_board_count": len(ranked_first_board_candidates),
        "ranked_first_board_recall_count": len(
            ranked_first_board_recall_candidates
        ),
        "ranked_first_board_actionable_count": sum(
            1 for item in ranked_first_board_candidates if item.get("prediction_actionable")
        ),
        "ranked_first_board_abstained_slots": max(
            canonical_formal_ranked_limit - len(ranked_first_board_candidates),
            0,
        ),
        "ranked_second_board_count": len(ranked_second_board_candidates),
        "ranked_second_board_actionable_count": sum(
            1 for item in ranked_second_board_candidates if item.get("trade_ready")
        ),
        "learning": {
            "evaluated_records": evaluated_learning_records,
            "recorded_predictions": recorded_predictions,
            "prediction_run_id": prediction_run_id,
            "bucket_count": len(learning_stats),
            "buckets": learning_stats,
            "notes": [
                "候选池会记录分赛道预测快照，并在后续K线/涨停池足够后自动评估成功或失败",
                "有足够样本的路由以历史后验命中率为中心，仅保留部分个股相对强弱，避免把排序分冒充真实概率",
                "失败样本会记录未涨停归因，供后续调参和页面复盘使用",
                "15:10/20:00及各盘中确认时点独立持久化；正式次日成绩优先使用20:00收盘快照，盘中批次只做独立确认复盘",
                "概率校准优先使用当前模型版本和近90日样本，旧版本仅作为封顶先验，避免历史规则淹没新版本",
                "收盘 schedule 候选池会在次日批量结算，用于计算漏选与召回；概率校准只使用 ranked 主榜样本，避免观察池和盘中确认污染命中率",
                "绝对低位不直接加分；中低位修复活跃、主营行业点火及三日资金形成组合后只提高首板/强涨证据，普通收涨概率使用独立留出 lift，不能拿首板 LR 直接上调",
                "直接个股消息按上一收盘与隔夜窗口分别计数，高影响/重复事件分别校准首板、收涨和强涨，仍不能绕过交易执行闸门",
            ],
        },
        "debug": {
            "mixed_ranked_candidates": mixed_ranked_candidates,
            "internal_candidate_limit": internal_candidate_limit,
            "notes": [
                "mixed_ranked_candidates 仅用于联调排查，不代表统一概率口径",
                "首板请使用 ranked_first_board_candidates，二板请使用 ranked_second_board_candidates",
                "ranked_first_board_candidates 是正式 Top12；ranked_first_board_recall_candidates 是 Top30 宽召回观察层，不代表可以买",
                "actual_limit_up_replay 用上一交易日预测记录回放今天实际首板/二板，定位未入池、分数低、排序挤掉或过滤原因",
                "prediction_health 在本次请求写入预测记录之前检查快照是否缺失，避免旧预测被误读为当天预测",
            ],
        },
    }
    cached_at = time.monotonic()
    _PROMOTION_LATEST_CANDIDATES_CACHE[cache_scope] = (cached_at, payload)
    response_payload = _project_promotion_candidates_payload(
        payload,
        compact=compact,
        candidate_limit=candidate_limit,
        ranked_limit=recall_ranked_limit,
    )
    if not is_schedule_snapshot:
        _PROMOTION_PAGE_CANDIDATES_CACHE[page_cache_key] = (cached_at, response_payload)
        await _persist_dashboard_snapshot(
            db,
            PROMOTION_PAGE_CANDIDATES_SNAPSHOT_KEY,
            second_board_trade_date,
            payload,
        )
    elif recorded_predictions:
        await _persist_dashboard_snapshot(
            db,
            PROMOTION_CANDIDATES_SNAPSHOT_KEY,
            second_board_trade_date,
            payload,
        )
    return response_payload


@router.get(
    "/batch-health",
    summary="正式预测窗口与不可变批次只读诊断",
    description=(
        "按请求交易日核对既有时间窗与已持久化正式尝试，区分缺批、开始阻断、"
        "完成但路线未通过。只读，不生成候选、不补跑过期窗口、不回退旧榜。"
    ),
)
async def promotion_batch_health(
    trade_date: date | None = None,
    db: AsyncSession = Depends(get_db),
):
    # Dedicated uncached SELECT-only endpoint: never call candidates, quality
    # collection, calendar network fallback, or ledger storage initializers here.
    from app.core.prediction_data_quality import PREDICTION_ROUTE_REQUIRED_DATASETS
    from app.promotion.batch_diagnostics import load_batch_diagnostics

    checked_at = datetime.now()
    requested_date = trade_date if trade_date is not None else checked_at.date()
    identity = get_promotion_model_identity()
    return await load_batch_diagnostics(
        db, trade_date=requested_date, checked_at=checked_at,
        expected_identity={
            "model_version": identity.active_model_version,
            "feature_version": identity.feature_version,
            "data_version": identity.data_version,
            "runtime_mode": identity.runtime_mode.value,
        },
        context_windows=_PROMOTION_OFFICIAL_CONTEXT_WINDOWS,
        required_routes=tuple(PREDICTION_ROUTE_REQUIRED_DATASETS),
        officially_closed=requested_date.weekday() >= 5 or is_official_closed_day(requested_date),
        canonical_close_contexts=PROMOTION_CANONICAL_CLOSE_CONTEXTS,
    )


@router.get(
    "/learning-review",
    summary="晋级预测逐日复盘",
    description=(
        "把前一交易日正式 schedule 主榜与下一交易日真实上涨、强涨、首板和二板逐日对齐，"
        "返回精度、召回率、概率误差和漏选原因。页面补位观察股不计入预测数。"
    ),
)
async def promotion_learning_review(
    lookback_days: int = PROMOTION_REVIEW_DEFAULT_DAYS,
    db: AsyncSession = Depends(get_db),
):
    normalized_days = max(
        3,
        min(_safe_int(lookback_days, PROMOTION_REVIEW_DEFAULT_DAYS), PROMOTION_REVIEW_MAX_DAYS),
    )
    cache_scope = _promotion_cache_scope(db)
    cache_key = (*cache_scope, normalized_days)
    cached = _PROMOTION_LEARNING_REVIEW_CACHE.get(cache_key)
    now = time.monotonic()
    if cached and now - cached[0] <= PROMOTION_PAGE_CACHE_TTL_SECONDS:
        payload = dict(cached[1])
        payload["cache_hit"] = True
        payload["cache_age_seconds"] = round(now - cached[0], 3)
        return payload
    payload = await _build_promotion_daily_learning_review(db, lookback_days=normalized_days)
    _PROMOTION_LEARNING_REVIEW_CACHE[cache_key] = (time.monotonic(), payload)
    return payload


@router.get(
    "/{code}/probability",
    summary="个股晋级概率查询",
    description=(
        "按目标阶段查询单只股票的概率口径。`target=1` 返回 5 日内首板概率，"
        "并复用首板候选池的 route / memory_features / sub_probabilities；"
        " `target=2` 返回次日晋级二板概率。"
    ),
)
async def promotion_probability(
    code: str,
    target: int = 2,
    db: AsyncSession = Depends(get_db),
):
    """晋级概率预测"""
    normalized_code = code.strip()
    evaluated_learning_records = await _refresh_promotion_learning(db)
    learning_stats = await _load_promotion_learning_stats(db)
    if evaluated_learning_records:
        await db.commit()
    if target == 1:
        first_board_snapshot = await prewarm_anomaly_snapshot(db, trade_date=None)
        first_board_trade_date = await _resolve_first_board_trade_date(db, first_board_snapshot)
        first_board_limit_ups = await _load_filtered_limit_ups(db, first_board_trade_date)
        first_board_limit_up_codes = {str(item.get("code") or "") for item in first_board_limit_ups}
        first_board_market_context = await _enrich_market_ladder_context(
            db,
            first_board_trade_date,
            _build_market_ladder_context(first_board_limit_ups),
        )
        candidates, _snapshot_time, _first_board_diagnostics = await _build_first_board_candidates(
            db,
            first_board_trade_date,
            first_board_limit_up_codes,
            first_board_market_context,
            2000,
            snapshot=first_board_snapshot,
        )
        candidates = _apply_promotion_learning_to_candidates(candidates, learning_stats)
        match = next((item for item in candidates if str(item.get("code") or "").strip() == normalized_code), None)
        if match is None:
            return {
                "code": normalized_code,
                "name": "",
                "trade_date": str(first_board_trade_date),
                "current_days": 0,
                "next_days": 1,
                "probability": 0,
                "confidence": CONFIDENCE_SCORES["low"],
                "confidence_level": "low",
                "confidence_label": CONFIDENCE_LABELS["low"],
                "main_probability_name": FIRST_BOARD_MAIN_PROBABILITY_NAME,
                "candidate_route": "",
                "candidate_route_label": "",
                "time_horizon": "",
                "time_horizon_label": "",
                "time_horizon_reason": "",
                "watch_bucket": "",
                "watch_bucket_label": "",
                "watch_bucket_reason": "",
                "support_squeeze_stage": "",
                "support_squeeze_stage_label": "",
                "support_squeeze_stage_reason": "",
                "support_squeeze_gap_label": "",
                "next_threshold_hint": "",
                "actionable_suggestions": [],
                "main_uptrend_score": 0.0,
                "main_uptrend_label": "",
                "main_uptrend_reason": "",
                "is_main_uptrend_ready": False,
                "sub_probabilities": {},
                "memory_features": {},
                "factors": {"note": "当前未进入首板候选池"},
            }
        match = _annotate_first_board_timing(match)
        return {
            "code": normalized_code,
            "name": str(match.get("name") or ""),
            "trade_date": str(first_board_trade_date),
            "current_days": 0,
            "next_days": 1,
            "probability": _safe_float(match.get("probability")),
            "confidence": _safe_float(match.get("confidence")),
            "confidence_level": str(match.get("confidence_level") or "low"),
            "confidence_label": str(match.get("confidence_label") or CONFIDENCE_LABELS["low"]),
            "main_probability_name": str(match.get("main_probability_name") or FIRST_BOARD_MAIN_PROBABILITY_NAME),
            "candidate_route": str(match.get("candidate_route") or ""),
            "candidate_route_label": str(match.get("candidate_route_label") or ""),
            "time_horizon": str(match.get("time_horizon") or ""),
            "time_horizon_label": str(match.get("time_horizon_label") or ""),
            "time_horizon_reason": str(match.get("time_horizon_reason") or ""),
            "watch_bucket": str(match.get("watch_bucket") or ""),
            "watch_bucket_label": str(match.get("watch_bucket_label") or ""),
            "watch_bucket_reason": str(match.get("watch_bucket_reason") or ""),
            "support_squeeze_stage": str(match.get("support_squeeze_stage") or ""),
            "support_squeeze_stage_label": str(match.get("support_squeeze_stage_label") or ""),
            "support_squeeze_stage_reason": str(match.get("support_squeeze_stage_reason") or ""),
            "support_squeeze_gap_label": str(match.get("support_squeeze_gap_label") or ""),
            "next_threshold_hint": str(match.get("next_threshold_hint") or ""),
            "actionable_suggestions": list(match.get("actionable_suggestions") or []),
            "main_uptrend_score": _safe_float(match.get("main_uptrend_score")),
            "main_uptrend_label": str(match.get("main_uptrend_label") or ""),
            "main_uptrend_reason": str(match.get("main_uptrend_reason") or ""),
            "is_main_uptrend_ready": bool(match.get("is_main_uptrend_ready")),
            "sub_probabilities": match.get("sub_probabilities") or {},
            "memory_features": match.get("memory_features") or {},
            "learning_stats": match.get("learning_stats") or {},
            "factors": match.get("probability_factors") or {},
        }

    second_board_trade_date = await _get_latest_limit_up_trade_date(db)
    limit_ups = await _load_filtered_limit_ups(db, second_board_trade_date)
    second_board_market_context = await _enrich_market_ladder_context(
        db,
        second_board_trade_date,
        _build_market_ladder_context(limit_ups),
    )
    second_board_candidates = await _build_second_board_candidates(
        db,
        second_board_trade_date,
        limit_ups,
        second_board_market_context,
        2000,
    )
    second_board_candidates = _apply_promotion_learning_to_candidates(second_board_candidates, learning_stats)
    match = next((item for item in second_board_candidates if str(item.get("code") or "").strip() == normalized_code), None)

    if match is None:
        return {
            "code": normalized_code,
            "name": "",
            "trade_date": str(second_board_trade_date),
            "current_days": 0,
            "next_days": 2,
            "probability": 0,
            "confidence": CONFIDENCE_SCORES["low"],
            "confidence_level": "low",
            "confidence_label": CONFIDENCE_LABELS["low"],
            "main_probability_name": SECOND_BOARD_MAIN_PROBABILITY_NAME,
            "candidate_route": "second_board_promotion",
            "candidate_route_label": FIRST_BOARD_ROUTE_LABELS["second_board_promotion"],
            "sub_probabilities": {},
            "memory_features": {},
            "dragon_tiger": _dragon_tiger_empty_context("当前不在首板池，未查询龙虎榜成员"),
            "factors": {"note": "当前不在最新交易日首板池，暂无次日晋级二板样本"},
        }

    return {
        "code": normalized_code,
        "name": str(match.get("name") or ""),
        "trade_date": str(second_board_trade_date),
        "current_days": 1,
        "next_days": 2,
        "probability": _safe_float(match.get("probability")),
        "confidence": _safe_float(match.get("confidence")),
        "confidence_level": str(match.get("confidence_level") or "low"),
        "confidence_label": str(match.get("confidence_label") or CONFIDENCE_LABELS["low"]),
        "main_probability_name": str(match.get("main_probability_name") or SECOND_BOARD_MAIN_PROBABILITY_NAME),
        "candidate_route": str(match.get("candidate_route") or "second_board_promotion"),
        "candidate_route_label": str(match.get("candidate_route_label") or FIRST_BOARD_ROUTE_LABELS["second_board_promotion"]),
        "sub_probabilities": match.get("sub_probabilities") or {},
        "memory_features": {},
        "dragon_tiger": match.get("dragon_tiger") or _dragon_tiger_empty_context(),
        "learning_stats": match.get("learning_stats") or {},
        "factors": match.get("probability_factors") or {},
    }
