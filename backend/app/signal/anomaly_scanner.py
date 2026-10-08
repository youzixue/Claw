"""异动扫描器 — 实时异动检测+信号驱动+推送触发

核心能力:
1. 全市场实时异动扫描(涨停/跌停/资金/突破/冲高回落)
2. MA/BOLL后端实时计算(内存缓存, 每日重置)
3. DragonHead板块成分股组装(board_cons+stock_spot+limit_up_pool)
4. BullScore连板评分链路(LimitUpTracker.predict_promotion→PromotionResult)
5. 竞价→明日预案闭环(AuctionData+BullScore+技术位)
6. 风控: 缩量涨停降级/次新股⚠️/一字板标注

数据源: stock_spot(实时) + stock_kline(日K) + limit_up_pool + fund_flow + auction_data
输出: 异动列表 + 信号评分 + 推送触发
"""

import asyncio
import json
import statistics
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from typing import Optional

import numpy as np
from loguru import logger
from sqlalchemy import select, func, and_, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import settings
from app.data.fund_flow_clock import evidence_clock, fund_clock_status, main_fund_values_valid
from app.data.main_fund import (
    fund_order_breakdown, load_current_main_fund_map, main_fund_source_supported,
    load_latest_main_fund_display_map,
    main_fund_display_evidence, freeze_anomaly_main_fund,
)
from app.data.main_fund_window import load_main_fund_window, fund_window_payload
from app.data.auction_evidence import auction_evidence_status, auction_latest_order
from app.core.trade_calendar import trade_calendar
from app.core.data_date import resolve_latest_trade_date
from app.news.catalyst import load_direct_stock_catalyst_map
from app.models.stock import (
    StockSpot, StockKline, LimitUpPool, FundFlow,
    AuctionData, StockTag, StockSectorMapping, BoardCons, SectorPersistence, SectorInfo,
)
from app.models.sector import SectorLifecycle
from app.signal.dragon_head import DragonHeadScanner, DragonHeadResult
from app.signal.bull_score import BullScoreModel, BullScoreResult
from app.signal.capital_anomaly import CapitalAnomalyDetector, CapitalAnomaly
from app.signal.breakthrough import BreakthroughDetector, BreakthroughSignal
from app.signal.chip_concentration import ChipConcentrationAnalyzer, ChipResult
from app.signal.limit_up_tracker import LimitUpTracker, PromotionResult
from app.signal.long_cycle_profile import build_long_cycle_profile


CORE_GROWTH_LOGIC_BY_CODE = {
    "002768": "高分子材料平台，改性塑料与复合材料需求扩张",
    "603530": "电网外绝缘复合化与海外电网投资",
    "603283": "半导体及消费电子自动化设备需求",
    "002518": "数据中心UPS与储能电源需求",
}


# 这些标签常见于大范围股票池或财务/资本属性，不足以证明 A 对 B 存在可交易的
# 产业联动。它们可以参与龙头辨识，但不能单独触发“看A做B”候选。
NON_CAUSAL_LINKAGE_SECTOR_TOKENS = (
    "中报预增",
    "年报预增",
    "业绩预增",
    "预盈预增",
    "扭亏为盈",
    "融资融券",
    "沪股通",
    "深股通",
    "国企改革",
    "地方国企改革",
    "央企改革",
    "股权转让",
    "回购增持",
    "证金持股",
    "机构重仓",
    "基金重仓",
    "养老金持股",
    "标普道琼斯",
    "MSCI",
    "同花顺漂亮100",
    "高股息",
    "破净股",
    "创投",
)

MAIN_WAVE_CORE_LIFECYCLE_STATES = {"emerging", "accelerating"}


def _safe_float(value, default: Optional[float] = 0.0) -> Optional[float]:
    """转换数值字段；调用方可传 ``default=None`` 保留缺失语义。"""
    if value is None:
        return default
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return default
    if np.isnan(numeric) or np.isinf(numeric):
        return default
    return numeric


def _ema_series(values: np.ndarray, period: int) -> np.ndarray:
    """Return a standard SMA-seeded EMA series without forward filling."""
    values = np.asarray(values, dtype=float)
    result = np.full(len(values), np.nan, dtype=float)
    if len(values) < period:
        return result
    alpha = 2.0 / (period + 1)
    result[period - 1] = float(np.mean(values[:period]))
    for index in range(period, len(values)):
        result[index] = alpha * values[index] + (1 - alpha) * result[index - 1]
    return result


def _latest_wilder_rsi(closes: np.ndarray, period: int = 14) -> float | None:
    """Calculate the latest RSI with Wilder smoothing.

    The seed intentionally uses all first ``period`` price differences.  The
    former detail-plan fallback of RSI=50 made missing data look like a real
    neutral/healthy signal and could accidentally confirm an aggressive plan.
    """
    closes = np.asarray(closes, dtype=float)
    if len(closes) < period + 1:
        return None
    deltas = np.diff(closes)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_gain = float(np.mean(gains[:period]))
    avg_loss = float(np.mean(losses[:period]))
    for index in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + float(gains[index])) / period
        avg_loss = (avg_loss * (period - 1) + float(losses[index])) / period
    if avg_loss == 0:
        return 50.0 if avg_gain == 0 else 100.0
    relative_strength = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + relative_strength))


def _kdj_series(
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    period: int = 9,
) -> tuple[np.ndarray, np.ndarray, np.ndarray] | None:
    """Calculate K/D/J with the project's 1/3 smoothing convention."""
    if len(closes) < period or len(highs) != len(closes) or len(lows) != len(closes):
        return None
    k_values = np.full(len(closes), np.nan, dtype=float)
    d_values = np.full(len(closes), np.nan, dtype=float)
    j_values = np.full(len(closes), np.nan, dtype=float)
    previous_k = 50.0
    previous_d = 50.0
    for index in range(period - 1, len(closes)):
        highest = float(np.max(highs[index - period + 1:index + 1]))
        lowest = float(np.min(lows[index - period + 1:index + 1]))
        rsv = 50.0 if highest == lowest else (closes[index] - lowest) / (highest - lowest) * 100.0
        previous_k = (2.0 * previous_k + rsv) / 3.0
        previous_d = (2.0 * previous_d + previous_k) / 3.0
        k_values[index] = previous_k
        d_values[index] = previous_d
        j_values[index] = 3.0 * previous_k - 2.0 * previous_d
    return k_values, d_values, j_values


def _derive_kline_boundary(tech: dict, price: float) -> dict:
    """从日K技术指标推导安全买入边际，供追高风险过滤使用。

    只依赖 `_calc_technical_indicators` 已产出的均线/近N日高点/累计涨幅字段，
    与盘中实时价换算成偏离度，不新增 DB 查询。返回空 dict 表示无法计算。
    """
    boundary: dict[str, float] = {}
    if price <= 0:
        return boundary
    ma20 = _safe_float(tech.get("ma20"))
    ma60 = _safe_float(tech.get("ma60"))
    high_20d = _safe_float(tech.get("high_20d"))
    high_60d = _safe_float(tech.get("high_60d"))
    high_120d = _safe_float(tech.get("high_120d"))
    if ma20 > 0:
        boundary["bias_to_ma20_pct"] = round((price / ma20 - 1.0) * 100.0, 2)
    if ma60 > 0:
        boundary["bias_to_ma60_pct"] = round((price / ma60 - 1.0) * 100.0, 2)
    if high_20d > 0:
        boundary["dist_to_high_20d_pct"] = round((price / high_20d - 1.0) * 100.0, 2)
    if high_60d > 0:
        boundary["dist_to_high_60d_pct"] = round((price / high_60d - 1.0) * 100.0, 2)
    if high_120d > 0:
        boundary["dist_to_high_120d_pct"] = round((price / high_120d - 1.0) * 100.0, 2)
    return_20d = _safe_float(tech.get("return_20d"))
    boundary["return_20d"] = round(return_20d, 2)
    # 近5日累计涨幅：以最近完整日收盘为基线，盘中现价与5个交易日前的收盘对比。
    recent_closes = tech.get("recent_closes") or []
    if len(recent_closes) >= 6:
        base_close = _safe_float(recent_closes[-6])
        if base_close > 0:
            boundary["return_5d"] = round((price / base_close - 1.0) * 100.0, 2)
    return boundary


def _has_display_sector_name(value: str | None) -> bool:
    """判断板块名是否适合直接展示，避免空名申万代码顶到页面。"""
    return bool(str(value or "").strip())


def _is_causal_trade_driver_sector(item: dict | None) -> bool:
    """只把可解释的同花顺产业/题材映射当作交易主驱动。"""
    factor = item or {}
    sector_name = str(factor.get("sector_name") or "").strip()
    if not sector_name and "source" not in factor and "sector_type" not in factor:
        # 兼容旧快照/纯函数输入；生产扫描的映射一定带名称与来源。
        return True
    if not sector_name or any(
        token in sector_name for token in NON_CAUSAL_LINKAGE_SECTOR_TOKENS
    ):
        return False
    # 生产数据带有source/type时严格校验；纯函数测试或旧快照缺少元数据时，
    # 只做语义黑名单降级，避免把历史快照全部判空。
    if "source" in factor or "sector_type" in factor:
        return bool(
            str(factor.get("source") or "") == "pywencai"
            and str(factor.get("sector_type") or "") in {"concept", "industry"}
        )
    return True


def _is_positive_sector_candidate(item: dict | None) -> bool:
    """板块当日是否算「正向」—— 概念与行业的资金流口径不同，门槛也不同。

    概念（`sector_type == "concept"`）
        要求**资金净流入为正 且 收涨**。`fund_flow` 是该概念板块自身的资金流，
        是最直接的证据，两道都要。

    行业（`sector_type == "industry"`）
        只要求**收涨**（`change_pct > 0`），`fund_flow` 正负不作硬门槛。
        原因是行业资金流不是该三级行业自己的数：
        `scheduler._update_sector_persistence` 写入时把东财**二级行业**的
        资金流按三级子行业数量均分
        （``per_code_flow = round(fund_flow / len(sub_codes), 2)``），
        三级行业拿到的是被摊薄后的份额。摊薄**不翻转符号**（除以正整数），
        所以负值仍是二级行业真净流出 —— 但它是**聚合量**而非该板块自身量，
        与概念口径不可比；且 1/N 后经 `round(..., 2)`，小正值会被抹成 0
        从而误判为非正向。故行业只用价格口径。

    2026-09-17 放宽的背景：银行/水电/煤炭等低波动大盘股身上，当日为正的
    标签几乎必是 `沪股通`/`融资融券`/`高股息精选`/`中特估100`/`证金持股`
    这类泛标签（已被 `is_excluded` 与 `NON_CAUSAL_LINKAGE_SECTOR_TOKENS`
    排除），而产业板块又常因摊薄后的资金流为负而一并落选，使 `driver_primary`
    退化为「暂无明确主驱动」，进而拿不到任何主驱动上下文。

    注意：这里只放宽 **上下文/展示** 的候选池。真正控制买点与推送的四个门槛
    （`_has_positive_sector_driver`、`_resolve_sector_repair_driver`、
    `_resolve_old_hot_repair_driver`、`tenbagger._has_positive_driver_sector`）
    仍各自要求 `fund_flow > 0`，本次未改 —— 即「显示有主驱动」不等于
    「买点门槛放行」，二者口径本就不同（放宽前实测 120 条有主驱动的票里
    仍有 4 条被门槛拦住）。
    **热路径**：`_build_sector_context_detail` 每轮 `scan_market` 会调本函数
    约 **5.8 万次**（实测 2026-09-16：4,943 只股票 × 平均 11.7 个板块）。
    故此处刻意**不用 `_safe_float`** —— 它带 try/except 与字符串处理，
    单次 1.63µs，是直接比较（0.15µs）的 11 倍，按调用量放大成约 **+90ms/轮**。
    传入的 `sectors` 元素由本函数上方同一函数用 `round(float(...), 2)` 构造，
    值必然是数值或 0，无需容错转换；`float(... or 0)` 与原内联实现口径一致。
    实测改用直接比较后每轮净增从 +90.7ms 降到 **+6.2ms**。
    """
    factor = item or {}
    if float(factor.get("change_pct") or 0) <= 0:
        return False
    # `sector_type` 由调用方写成 str；直接相等比较即可，省掉每次 str() 分配。
    if factor.get("sector_type") == "industry":
        return True
    return float(factor.get("fund_flow") or 0) > 0


def _select_reference_factors(candidates: list[dict], limit: int = 2) -> list[dict]:
    """选取「参考板块」，并**保证该股自己的行业在里面且排首位**。

    背景（2026-09-17）
    -----------------
    `reference_candidates` 已按 `relevance_score` 降序，但打分里概念有 +2.0
    基础分、行业只有 +0.8（见 `_build_sector_context_detail`），所以行业几乎
    总被概念挤出前 2 名。实测 `601088 中国神华` 的
    `煤炭-煤炭开采加工-煤炭开采`（当日 -0.25%）**完全没进参考列表**，
    于是页面无法回答「这只票为什么没有主驱动」—— 答案恰恰是它自己的行业
    当日没转强。

    行业才是「该股所属产业当日状态」的直接答案，故提到首位；概念参考保留
    在后。没有行业候选时维持原有顺序。

    纯上下文/展示用途：不参与 `selected_sectors`（主驱动）的选取，也不影响
    任何买点门槛。
    """
    if not candidates:
        return []
    industry = next(
        (
            item
            for item in candidates
            if str(item.get("sector_type") or "") == "industry"
        ),
        None,
    )
    if industry is None:
        return list(candidates[:limit])
    # 取所有概念参考后把行业插到最前；行业若已被选中也要提前（不能只判断
    # 「在不在列表里」—— 顺序才是展示效果的来源）。
    industry_code = str(industry.get("sector_code") or "")
    rest = [
        item
        for item in candidates
        if str(item.get("sector_code") or "") != industry_code
    ]
    return [industry, *rest][:limit]


def _is_tradeable_linkage_sector(
    sector_meta: dict | None,
    persistence: SectorPersistence | None,
) -> bool:
    """A→B 只使用有当日扩散和资金确认的可交易概念板块。"""
    meta = sector_meta or {}
    if not _is_causal_trade_driver_sector(meta) or meta.get("sector_type") != "concept":
        return False
    if persistence is None:
        return False
    return bool(
        _safe_float(persistence.strength_score) >= 60
        and _safe_float(persistence.change_pct) >= 1.0
        and _safe_float(persistence.fund_flow) > 0
        and int(persistence.limit_up_count or 0) >= 2
    )


def _is_main_wave_core_sector_factor(item: dict | None) -> bool:
    """主升回踩只采信有产业含义、处在正向周期且真实扩散的板块。"""
    factor = item or {}
    sector_name = str(factor.get("sector_name") or "").strip()
    lifecycle_state = str(factor.get("lifecycle_state") or "").strip().lower()
    if (
        not _is_causal_trade_driver_sector(factor)
        or lifecycle_state not in MAIN_WAVE_CORE_LIFECYCLE_STATES
    ):
        return False
    return bool(
        _safe_float(factor.get("lifecycle_score")) >= 60.0
        and int(factor.get("active_days") or 0) >= 2
        and _safe_float(factor.get("strength_score")) >= 60.0
        and _safe_float(factor.get("change_pct")) >= 0.8
        and _safe_float(factor.get("fund_flow")) > 0
        and int(factor.get("limit_up_count") or 0) >= 2
    )


def _resolve_main_wave_core_sector_driver(sector_context: dict | None) -> dict | None:
    """返回主升回踩可用的最强产业板块；泛概念和退潮板块不参与。"""
    factors = [
        dict(item)
        for item in ((sector_context or {}).get("sector_factors") or [])
        if _is_main_wave_core_sector_factor(item)
    ]
    if not factors:
        return None
    factors.sort(
        key=lambda item: (
            bool(item.get("is_sector_leader")),
            _safe_float(item.get("lifecycle_score")),
            _safe_float(item.get("strength_score")),
            int(item.get("limit_up_count") or 0),
            _safe_float(item.get("fund_flow")),
        ),
        reverse=True,
    )
    return factors[0]


def _build_large_order_inflow_context(
    current_fund: dict | None,
    *,
    traded_amount: float = 0.0,
) -> dict:
    """按绝对金额+成交额占比确认大单流入，避免只用净占比误判小额脉冲。"""
    fund = current_fund or {}
    main_net = _safe_float(fund.get("main_net_inflow"))
    main_pct = _safe_float(fund.get("main_net_inflow_pct"))
    breakdown = fund_order_breakdown(fund)
    super_net = breakdown["super_net_inflow"]
    big_net = breakdown["big_net_inflow"]
    # 缺失超大单可能是真实净流出；不可吞0后仅以正大单冒充合计。
    # 同样不从主力净额反推缺失细分。原金额/占比阈值不变。
    large_order_net = (super_net + big_net
                       if super_net is not None and big_net is not None else None)
    if large_order_net is not None and not np.isfinite(large_order_net):
        large_order_net = None
    amount = max(_safe_float(traded_amount), 0.0)
    min_main_amount = max(
        settings.ANOMALY_MAIN_WAVE_PULLBACK_MIN_MAIN_INFLOW_AMOUNT,
        min(amount * 0.01, 5e7),
    )
    min_large_order_amount = max(
        settings.ANOMALY_MAIN_WAVE_PULLBACK_MIN_LARGE_ORDER_INFLOW_AMOUNT,
        min(amount * 0.004, 2e7),
    )
    is_stale = bool(fund.get("is_stale"))
    confirmed = bool(
        not is_stale
        and main_net >= min_main_amount
        and main_pct >= settings.ANOMALY_MAIN_WAVE_PULLBACK_MIN_MAIN_INFLOW_PCT
        and large_order_net is not None
        and large_order_net >= min_large_order_amount
    )
    return {
        "large_order_inflow_confirmed": confirmed,
        "large_order_net_inflow": round(large_order_net, 2) if large_order_net is not None else None,
        "large_order_breakdown_status": "known" if large_order_net is not None else "unknown",
        "large_order_min_main_inflow": round(min_main_amount, 2),
        "large_order_min_inflow": round(min_large_order_amount, 2),
    }


def _spot_volume_in_kline_unit(value: float | int | None) -> float:
    """Tencent spot成交量为手，同花顺日K成交量为股。"""
    return _safe_float(value) * 100.0


def _spot_effective_min5_change(spot: StockSpot) -> float:
    """数值展示兼容层；交易确认必须同时检查 `_spot_min5_available`。"""
    value = (
        getattr(spot, "_effective_min5_change", None)
        if hasattr(spot, "_effective_min5_change")
        else getattr(spot, "min5_change", None)
    )
    numeric = _safe_float(value, None)
    return numeric if numeric is not None else -999.0


def _spot_min5_available(spot: StockSpot) -> bool:
    value = (
        getattr(spot, "_effective_min5_change", None)
        if hasattr(spot, "_effective_min5_change")
        else getattr(spot, "min5_change", None)
    )
    return _safe_float(value, None) is not None


def _validated_min5(value) -> Optional[float]:
    numeric = _safe_float(value, None)
    return numeric if numeric is not None and abs(numeric) <= 10.0 else None


def _numeric_or_default(value, default: float) -> float:
    """只在真正缺失/非法时回退；合法 ``0.0`` 必须原样保留。"""
    numeric = _safe_float(value, None)
    return default if numeric is None else numeric


def _as_quote_datetime(value) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def _spot_observed_at(spot: StockSpot, override: datetime | None = None) -> datetime:
    """优先使用源行情时点，旧行依次回退接收/提交时点。"""
    if isinstance(override, datetime):
        return override
    for field_name in ("source_quote_at", "received_at", "updated_at"):
        value = _as_quote_datetime(getattr(spot, field_name, None))
        if value is not None:
            return value
    return datetime.now()


def _spot_is_fresh_for_live_scan(
    spot: StockSpot,
    *,
    now: datetime,
    trade_date: date,
) -> bool:
    """源时点可用时以源时点判新鲜；旧行才回退接收/提交时点。"""
    observed_at = _spot_observed_at(spot)
    age_sec = (now - observed_at).total_seconds()
    return bool(
        observed_at.date() == trade_date
        and -max(1, settings.ANOMALY_QUOTE_ROUND_TOLERANCE_SEC)
        <= age_sec
        <= settings.ANOMALY_QUOTE_MAX_AGE_SEC
    )


@dataclass
class AnomalyEvent:
    """异动事件"""
    code: str
    name: str
    event_type: str           # limit_up/limit_down/capital/breakthrough/pump_dump/volume
    level: str                # critical/major/minor
    score: float
    detail: dict = field(default_factory=dict)
    description: str = ""
    risk_warnings: list = field(default_factory=list)
    is_ipo_recent: bool = False    # 次新股⚠️
    is_one_word_board: bool = False  # 一字板


class AnomalyScanner:
    """异动扫描器 — 统一入口"""

    def __init__(self):
        self.capital_detector = CapitalAnomalyDetector()
        self.breakthrough_detector = BreakthroughDetector()
        self.dragon_scanner = DragonHeadScanner()
        self.chip_analyzer = ChipConcentrationAnalyzer()
        self.bull_model = BullScoreModel()
        self.limit_up_tracker = LimitUpTracker()

        # MA/BOLL缓存: {code: {ma5, ma10, ma20, ma60, boll_upper, high_20d, high_60d, high_120d, avg_vol_20d}}
        self._tech_cache: dict[str, dict] = {}
        self._cache_date: Optional[str] = None  # 缓存日期(每日重置)
        self._quote_history: dict[str, list[tuple[datetime, float]]] = {}
        self._quote_history_date: Optional[date] = None
        self._rolling_60s_history: dict[str, list[tuple[datetime, float, float]]] = {}
        self._rolling_60s_history_date: Optional[date] = None
        self._amount_flow_state: dict[str, dict] = {}
        self._amount_flow_state_date: Optional[date] = None
        self._intraday_reversal_state: dict[str, dict] = {}
        self._intraday_reversal_state_date: Optional[date] = None
        # stock_spot 是单行 upsert；扫描繁忙时先保存已提交批次的最小叶子字段，
        # 下一轮按源行情时点回放给状态机，避免 A/B 被最新 C 覆盖。
        self._quote_batch_inbox: deque[dict] = deque()
        self.low_base_watchlist_codes = {
            code.strip()
            for code in str(settings.ANOMALY_LOW_BASE_WATCHLIST or "").split(",")
            if code.strip()
        }
        # 由明日预案盘后计算动态注入；仅保存当时可见的形态和支撑/压力快照。
        self.dynamic_trend_pool: dict[str, dict] = {}

    def update_dynamic_trend_pool(self, candidates: list[dict] | None) -> None:
        """更新趋势驱动观察池，供下一轮盘中异动扫描使用。"""
        normalized: dict[str, dict] = {}
        for item in candidates or []:
            code = str(item.get("code") or "").strip()
            if not code:
                continue
            stats = dict(item.get("main_wave_stats") or {})
            support = _safe_float(item.get("support") or stats.get("support"))
            resistance = _safe_float(item.get("resistance") or stats.get("resistance"))
            if support <= 0 or resistance <= 0 or resistance <= support:
                continue
            normalized[code] = {
                "code": code,
                "name": str(item.get("name") or code),
                "score": _safe_float(item.get("main_wave_score") or item.get("score")),
                "label": str(item.get("candidate_source_label") or item.get("label") or "趋势驱动候选"),
                "source": str(item.get("candidate_source") or item.get("source") or "trend_driver_setup"),
                "setup_state": str(item.get("setup_state") or stats.get("setup_state") or "candidate"),
                "support": support,
                "resistance": resistance,
                "ma5": _safe_float(stats.get("ma5")),
                "ma10": _safe_float(stats.get("ma10")),
                "ma20": _safe_float(stats.get("ma20")),
                "stats": stats,
            }
        self.dynamic_trend_pool = normalized
        logger.info(f"动态趋势/连板前兆池更新: {len(normalized)}只")

    def enqueue_quote_batch(self, quotes) -> int:
        """保存一轮已提交行情的最小状态机输入；不持有 ORM 或数据库引用。"""
        fields = (
            "code", "name", "price", "prev_close", "low", "change_pct", "amount",
            "source_quote_at", "received_at", "updated_at", "quote_round_id",
        )
        owned: list[dict] = []
        for quote in quotes or []:
            if not isinstance(quote, dict):
                continue
            item = {field_name: quote.get(field_name) for field_name in fields}
            if not str(item.get("code") or "") or _safe_float(item.get("price")) <= 0:
                continue
            owned.append(item)
        if not owned:
            return 0
        owned.sort(
            key=lambda item: (
                _as_quote_datetime(item.get("source_quote_at"))
                or _as_quote_datetime(item.get("received_at"))
                or _as_quote_datetime(item.get("updated_at"))
                or datetime.min,
                str(item.get("code") or ""),
            )
        )
        self._quote_batch_inbox.append({
            "enqueued_at": datetime.now(),
            "quotes": owned,
        })
        max_batches = max(int(settings.ANOMALY_QUOTE_INBOX_MAX_BATCHES), 1)
        dropped = 0
        while len(self._quote_batch_inbox) > max_batches:
            self._quote_batch_inbox.popleft()
            dropped += 1
        if dropped:
            logger.warning(f"异动行情批次 inbox 已满，丢弃最旧批次 {dropped} 个")
        return len(owned)

    def restore_quote_batches(self, batches: list[list[dict]]) -> int:
        """同日重启时直接预热连续状态机，不等待下一次重型扫描，也不产生事件。"""
        pending = [
            item
            for batch in (batches or [])
            for item in (batch or [])
            if isinstance(item, dict)
        ]
        pending.sort(
            key=lambda item: (
                _as_quote_datetime(item.get("source_quote_at"))
                or _as_quote_datetime(item.get("received_at"))
                or _as_quote_datetime(item.get("updated_at"))
                or datetime.min,
                str(item.get("code") or ""),
            )
        )
        restored = 0
        for item in pending:
            code = str(item.get("code") or "")
            if not code or _safe_float(item.get("price")) <= 0:
                continue
            spot = SimpleNamespace(**item)
            observed_at = _spot_observed_at(spot)
            self._track_intraday_min5_change(spot, observed_at)
            self._track_intraday_amount_flow(spot, observed_at)
            self._track_rolling_60s_momentum(spot, observed_at)
            self._track_intraday_reversal_state(spot, observed_at)
            restored += 1
        if restored:
            logger.info(f"异动状态已从分时归档恢复: {len(batches)}轮/{restored}条")
        return restored

    def _drain_quote_batch_inbox(
        self,
        *,
        blocked_stock_set: set[str],
        today: date,
        allowed_codes: set[str] | None = None,
        now: datetime | None = None,
    ) -> int:
        """按真实行情时点预热四个连续状态机，事件检测仍使用数据库最新轮。"""
        now = now or datetime.now()
        max_age_sec = max(int(settings.ANOMALY_QUOTE_INBOX_MAX_AGE_SEC), 1)
        pending: list[dict] = []
        dropped_batches = 0
        while self._quote_batch_inbox:
            batch = self._quote_batch_inbox.popleft()
            enqueued_at = _as_quote_datetime(batch.get("enqueued_at")) or now
            if (now - enqueued_at).total_seconds() > max_age_sec:
                dropped_batches += 1
                continue
            pending.extend(batch.get("quotes") or [])
        if dropped_batches:
            logger.warning(f"异动行情批次 inbox 过期丢弃 {dropped_batches} 个批次")
        pending.sort(
            key=lambda item: (
                _as_quote_datetime(item.get("source_quote_at"))
                or _as_quote_datetime(item.get("received_at"))
                or _as_quote_datetime(item.get("updated_at"))
                or datetime.min,
                str(item.get("code") or ""),
            )
        )
        tracked = 0
        for item in pending:
            code = str(item.get("code") or "")
            name = str(item.get("name") or "")
            if (
                code in blocked_stock_set
                or (allowed_codes is not None and code not in allowed_codes)
                or "ST" in name.upper()
                or "退" in name
            ):
                continue
            spot = SimpleNamespace(**item)
            quote_time = _spot_observed_at(spot)
            if quote_time.date() != today:
                continue
            self._track_intraday_min5_change(spot, quote_time)
            self._track_intraday_amount_flow(spot, quote_time)
            self._track_rolling_60s_momentum(spot, quote_time)
            self._track_intraday_reversal_state(spot, quote_time)
            tracked += 1
        return tracked

    def track_intraday_min5_change(
        self,
        spot: StockSpot,
        now: datetime | None = None,
    ) -> Optional[float]:
        """用连续spot快照计算真实5分钟涨跌；窗口未形成时返回None。

        腾讯实时接口没有可信的5分钟涨跌字段。调用方若要把该值用于交易确认，
        必须区分“真实横盘0%”与“尚无5分钟基线”，不能把数据库占位0当成确认。
        """
        quote_time = _spot_observed_at(spot, now)
        quote_date = quote_time.date()
        if self._quote_history_date is None or quote_date > self._quote_history_date:
            self._quote_history.clear()
            self._quote_history_date = quote_date
        elif quote_date < self._quote_history_date:
            # stock_spot会保留停牌/ST等旧快照，旧日期不能反向清空当日全市场轨迹。
            return None

        code = str(getattr(spot, "code", "") or "")
        price = _safe_float(getattr(spot, "price", 0))
        if not code or price <= 0:
            return None

        history = self._quote_history.setdefault(code, [])
        if history and quote_time < history[-1][0]:
            # 同日乱序快照不参与动能计算，也不能污染后续基线。
            return None
        if history and history[-1][0] == quote_time:
            history[-1] = (quote_time, price)
        else:
            history.append((quote_time, price))
        cutoff = quote_time - timedelta(minutes=8)
        history[:] = [item for item in history if item[0] >= cutoff]

        baseline_cutoff = quote_time - timedelta(minutes=4, seconds=30)
        baseline_floor = quote_time - timedelta(minutes=5, seconds=30)
        baselines = [
            item for item in history
            if baseline_floor <= item[0] <= baseline_cutoff
        ]
        if not baselines:
            return None
        baseline_price = baselines[-1][1]
        if baseline_price <= 0:
            return None
        result = (price / baseline_price - 1.0) * 100.0
        return round(result, 3) if abs(result) <= 10.0 else None

    def _track_intraday_min5_change(
        self,
        spot: StockSpot,
        now: datetime | None = None,
    ) -> Optional[float]:
        """保留缺失语义；没有4分30秒至5分30秒基线时返回 ``None``。"""
        return self.track_intraday_min5_change(spot, now)

    def _track_rolling_60s_momentum(
        self,
        spot: StockSpot,
        now: datetime | None = None,
    ) -> dict:
        """计算同一滚动约60秒窗口的价格涨速和增量成交额。

        腾讯全市场行情通常30秒一轮，因此允许基准快照在目标60秒前后15秒内。
        价格与成交额必须使用同一基准快照，避免把30秒成交放量错误配给60秒涨幅。
        """
        quote_time = _spot_observed_at(spot, now)
        empty_result = {
            "rolling_60s_confirmed": False,
            "rolling_60s_change_pct": 0.0,
            "rolling_60s_interval_sec": 0.0,
            "rolling_60s_amount_delta": 0.0,
            "rolling_60s_amount_pace_ratio": 0.0,
            "rolling_60s_amount_reference": "",
            "rolling_60s_close_position": 0.0,
            "rolling_60s_peak_pullback_pct": 0.0,
            "rolling_60s_up_leg_ratio": 0.0,
            "rolling_60s_path_confirmed": False,
            "rolling_60s_tier": "",
        }
        quote_date = quote_time.date()
        if (
            self._rolling_60s_history_date is None
            or quote_date > self._rolling_60s_history_date
        ):
            self._rolling_60s_history.clear()
            self._rolling_60s_history_date = quote_date
        elif quote_date < self._rolling_60s_history_date:
            setattr(spot, "_rolling_60s_momentum", empty_result)
            return dict(empty_result)

        code = str(getattr(spot, "code", "") or "")
        price = _safe_float(getattr(spot, "price", 0))
        amount = _safe_float(getattr(spot, "amount", 0))
        if not code or price <= 0 or amount <= 0:
            setattr(spot, "_rolling_60s_momentum", empty_result)
            return dict(empty_result)

        history = self._rolling_60s_history.setdefault(code, [])
        if history and quote_time < history[-1][0]:
            setattr(spot, "_rolling_60s_momentum", empty_result)
            return dict(empty_result)
        if history and quote_time == history[-1][0]:
            history[-1] = (quote_time, price, amount)
        else:
            history.append((quote_time, price, amount))

        window_sec = max(int(settings.ANOMALY_ROLLING_60S_WINDOW_SEC), 1)
        tolerance_sec = max(int(settings.ANOMALY_ROLLING_60S_TOLERANCE_SEC), 0)
        retention_cutoff = quote_time - timedelta(
            seconds=max(
                int(settings.ANOMALY_ROLLING_60S_HISTORY_SEC),
                window_sec + tolerance_sec + 30,
            )
        )
        history[:] = [item for item in history if item[0] >= retention_cutoff]

        candidates: list[tuple[float, tuple[datetime, float, float]]] = []
        for item in history[:-1]:
            interval_sec = (quote_time - item[0]).total_seconds()
            if window_sec - tolerance_sec <= interval_sec <= window_sec + tolerance_sec:
                candidates.append((abs(interval_sec - window_sec), item))
        if not candidates:
            setattr(spot, "_rolling_60s_momentum", empty_result)
            return dict(empty_result)

        _, (baseline_time, baseline_price, baseline_amount) = min(
            candidates,
            key=lambda item: item[0],
        )
        interval_sec = (quote_time - baseline_time).total_seconds()
        amount_delta = amount - baseline_amount
        elapsed_before = self._trading_elapsed_seconds(baseline_time)
        if (
            baseline_price <= 0
            or amount_delta <= 0
            or elapsed_before < 60.0
            or interval_sec <= 0
        ):
            setattr(spot, "_rolling_60s_momentum", empty_result)
            return dict(empty_result)

        # 判定与对外字段统一保留3位小数，避免精确0.30%因
        # IEEE-754计算成0.299999...而漏报，同时不扩大0.299%的门槛。
        change_pct = round((price / baseline_price - 1.0) * 100.0, 3)
        if abs(change_pct) > 10.0:
            setattr(spot, "_rolling_60s_momentum", empty_result)
            return dict(empty_result)
        recent_amount_rate = amount_delta / interval_sec
        # 早盘累计成交额含集合竞价，直接用“累计金额/时间”会把
        # 分时基准抬得过高。有足够历史时，改用基准快照之前各有效
        # 增量窗口速率的中位数；冷启动时才回退到日内均速。
        prior_rates: list[float] = []
        prior_items = [item for item in history if item[0] <= baseline_time]
        for previous, current in zip(prior_items, prior_items[1:]):
            prior_interval = (current[0] - previous[0]).total_seconds()
            prior_delta = current[2] - previous[2]
            if 10.0 <= prior_interval <= 120.0 and prior_delta > 0:
                prior_rates.append(prior_delta / prior_interval)
        if len(prior_rates) >= 2:
            prior_amount_rate = statistics.median(prior_rates[-5:])
            amount_reference = "recent_median"
        else:
            prior_amount_rate = baseline_amount / elapsed_before
            amount_reference = "intraday_average"
        pace_ratio = recent_amount_rate / prior_amount_rate if prior_amount_rate > 0 else 0.0

        window_items = [item for item in history if baseline_time <= item[0] <= quote_time]
        window_prices = [item[1] for item in window_items if item[1] > 0]
        window_low = min(window_prices) if window_prices else baseline_price
        window_high = max(window_prices) if window_prices else price
        if window_high > window_low:
            close_position = (price - window_low) / (window_high - window_low)
        else:
            close_position = 1.0
        peak_pullback_pct = (
            max((window_high / price - 1.0) * 100.0, 0.0) if price > 0 else 99.0
        )
        legs = [
            current[1] - previous[1]
            for previous, current in zip(window_items, window_items[1:])
        ]
        up_leg_ratio = (
            sum(1 for value in legs if value > 0) / len(legs) if legs else 1.0
        )
        path_confirmed = bool(
            close_position >= settings.ANOMALY_ROLLING_60S_MIN_CLOSE_POSITION
            and peak_pullback_pct
            <= settings.ANOMALY_ROLLING_60S_MAX_PEAK_PULLBACK_PCT
            and up_leg_ratio >= settings.ANOMALY_ROLLING_60S_MIN_UP_LEG_RATIO
        )
        confirmed = bool(
            change_pct >= settings.ANOMALY_ROLLING_60S_MIN_CHANGE_PCT
            and amount_delta >= settings.ANOMALY_ACCELERATION_MIN_AMOUNT_DELTA
            and pace_ratio >= settings.ANOMALY_ACCELERATION_MIN_AMOUNT_PACE_RATIO
            and path_confirmed
        )
        tier = (
            "strong"
            if change_pct >= settings.ANOMALY_ROLLING_60S_STRONG_CHANGE_PCT
            else "medium"
            if change_pct >= settings.ANOMALY_ROLLING_60S_MEDIUM_CHANGE_PCT
            else "watch"
            if change_pct >= settings.ANOMALY_ROLLING_60S_MIN_CHANGE_PCT
            else ""
        )
        result = {
            "rolling_60s_confirmed": confirmed,
            "rolling_60s_change_pct": change_pct,
            "rolling_60s_interval_sec": round(interval_sec, 1),
            "rolling_60s_amount_delta": round(amount_delta, 2),
            "rolling_60s_amount_pace_ratio": round(pace_ratio, 3),
            "rolling_60s_amount_reference": amount_reference,
            "rolling_60s_close_position": round(close_position, 3),
            "rolling_60s_peak_pullback_pct": round(peak_pullback_pct, 3),
            "rolling_60s_up_leg_ratio": round(up_leg_ratio, 3),
            "rolling_60s_path_confirmed": path_confirmed,
            "rolling_60s_tier": tier,
        }
        setattr(spot, "_rolling_60s_momentum", result)
        return dict(result)

    @staticmethod
    def _trading_elapsed_seconds(quote_time: datetime) -> float:
        """返回截至快照时刻的连续竞价秒数，午休时间不计入成交速率。"""
        morning_start = quote_time.replace(hour=9, minute=30, second=0, microsecond=0)
        morning_end = quote_time.replace(hour=11, minute=30, second=0, microsecond=0)
        afternoon_start = quote_time.replace(hour=13, minute=0, second=0, microsecond=0)
        afternoon_end = quote_time.replace(hour=15, minute=0, second=0, microsecond=0)
        if quote_time <= morning_start:
            return 0.0
        if quote_time <= morning_end:
            return (quote_time - morning_start).total_seconds()
        morning_seconds = (morning_end - morning_start).total_seconds()
        if quote_time < afternoon_start:
            return morning_seconds
        return morning_seconds + min(
            (quote_time - afternoon_start).total_seconds(),
            (afternoon_end - afternoon_start).total_seconds(),
        )

    def _track_intraday_amount_flow(
        self,
        spot: StockSpot,
        now: datetime | None = None,
    ) -> dict:
        """比较最近一轮成交额速率与此前日内均速，验证拉升是否真实带量。"""
        quote_time = _spot_observed_at(spot, now)
        empty_result = {
            "intraday_amount_confirmed": False,
            "intraday_amount_delta": 0.0,
            "intraday_amount_interval_sec": 0.0,
            "intraday_amount_pace_ratio": 0.0,
        }
        quote_date = quote_time.date()
        if self._amount_flow_state_date is None or quote_date > self._amount_flow_state_date:
            self._amount_flow_state.clear()
            self._amount_flow_state_date = quote_date
        elif quote_date < self._amount_flow_state_date:
            setattr(spot, "_intraday_amount_flow", empty_result)
            return dict(empty_result)

        code = str(getattr(spot, "code", "") or "")
        amount = _safe_float(getattr(spot, "amount", 0))
        if not code or amount <= 0:
            setattr(spot, "_intraday_amount_flow", empty_result)
            return dict(empty_result)

        previous = dict(self._amount_flow_state.get(code) or {})
        previous_time = previous.get("last_time")
        if isinstance(previous_time, datetime) and quote_time < previous_time:
            setattr(spot, "_intraday_amount_flow", empty_result)
            return dict(empty_result)
        if previous_time == quote_time:
            result = dict(previous.get("last_result") or empty_result)
            setattr(spot, "_intraday_amount_flow", result)
            return result

        result = dict(empty_result)
        previous_amount = _safe_float(previous.get("last_amount"))
        if isinstance(previous_time, datetime) and previous_amount > 0:
            interval_sec = (quote_time - previous_time).total_seconds()
            amount_delta = amount - previous_amount
            elapsed_before = self._trading_elapsed_seconds(previous_time)
            # 30秒采集允许少量抖动；跨午休、长时间停更或累计成交额回退均重新预热。
            if 15.0 <= interval_sec <= 180.0 and amount_delta > 0 and elapsed_before >= 60.0:
                prior_amount_rate = previous_amount / elapsed_before
                recent_amount_rate = amount_delta / interval_sec
                pace_ratio = (
                    recent_amount_rate / prior_amount_rate
                    if prior_amount_rate > 0
                    else 0.0
                )
                confirmed = bool(
                    amount_delta >= settings.ANOMALY_ACCELERATION_MIN_AMOUNT_DELTA
                    and pace_ratio
                    >= settings.ANOMALY_ACCELERATION_MIN_AMOUNT_PACE_RATIO
                )
                result = {
                    "intraday_amount_confirmed": confirmed,
                    "intraday_amount_delta": round(amount_delta, 2),
                    "intraday_amount_interval_sec": round(interval_sec, 1),
                    "intraday_amount_pace_ratio": round(pace_ratio, 3),
                }

        self._amount_flow_state[code] = {
            "last_time": quote_time,
            "last_amount": amount,
            "last_result": result,
        }
        setattr(spot, "_intraday_amount_flow", result)
        return dict(result)

    @staticmethod
    def _has_acceleration_distribution_risk(spot: StockSpot) -> bool:
        """识别撤单、卖盘压制等冲高诱多特征。"""
        withdrawal_ratio = _safe_float(getattr(spot, "withdrawal_ratio", 0))
        imbalance = _safe_float(getattr(spot, "orderbook_imbalance", 0))
        bid_depth_5 = _safe_float(getattr(spot, "bid_depth_5", 0))
        ask_depth_5 = _safe_float(getattr(spot, "ask_depth_5", 0))
        return bool(
            withdrawal_ratio >= settings.ANOMALY_ACCELERATION_MAX_WITHDRAWAL_RATIO
            or imbalance <= -0.15
            or (
                bid_depth_5 > 0
                and ask_depth_5 > bid_depth_5 * 1.35
            )
        )

    def _track_intraday_reversal_state(
        self,
        spot: StockSpot,
        now: datetime | None = None,
    ) -> dict:
        """记录跨快照的水下到水上路径，避免只看当前涨幅错过快速反转。"""
        quote_time = _spot_observed_at(spot, now)
        quote_date = quote_time.date()
        if (
            self._intraday_reversal_state_date is None
            or quote_date > self._intraday_reversal_state_date
        ):
            self._intraday_reversal_state.clear()
            self._intraday_reversal_state_date = quote_date
        elif quote_date < self._intraday_reversal_state_date:
            return {}

        code = str(getattr(spot, "code", "") or "")
        price = _safe_float(getattr(spot, "price", 0))
        prev_close = _safe_float(getattr(spot, "prev_close", 0))
        current_change_pct = _safe_float(getattr(spot, "change_pct", 0))
        low_price = _safe_float(getattr(spot, "low", 0))
        if not code or price <= 0 or prev_close <= 0:
            return {}

        low_change_pct = (
            (low_price / prev_close - 1.0) * 100.0
            if low_price > 0
            else current_change_pct
        )
        previous = dict(self._intraday_reversal_state.get(code) or {})
        previous_change_pct = _safe_float(
            previous.get("last_change_pct"),
            current_change_pct,
        )
        previous_time = previous.get("last_time")
        if isinstance(previous_time, datetime) and quote_time < previous_time:
            return {}
        same_quote = previous_time == quote_time
        observation_count = int(previous.get("observation_count") or 0)
        if not same_quote:
            observation_count += 1

        # min_change_pct 继续保留交易所日内最低价的展示口径；武装状态只允许
        # 由本进程逐帧观察到的 current_change_pct 触发，禁止晚启动时用历史 low 前视。
        min_change_pct = min(
            _safe_float(previous.get("min_change_pct"), current_change_pct),
            current_change_pct,
            low_change_pct,
        )
        observed_min_change_pct = min(
            _safe_float(previous.get("observed_min_change_pct"), current_change_pct),
            current_change_pct,
        )
        was_underwater = bool(previous.get("was_underwater")) or (
            observed_min_change_pct
            <= settings.ANOMALY_UNDERWATER_REVERSAL_ARM_CHANGE_PCT
        )
        previous_crossed = bool(previous.get("crossed_from_underwater"))
        if current_change_pct <= settings.ANOMALY_UNDERWATER_REVERSAL_RESET_CHANGE_PCT:
            crossed_from_underwater = False
        elif previous_crossed:
            # 翻红后短暂回踩不应让下一轮扫描擦除已经观察到的路径。
            crossed_from_underwater = True
        else:
            crossed_from_underwater = bool(
                was_underwater
                and previous_change_pct <= 0.0
                and current_change_pct
                >= settings.ANOMALY_UNDERWATER_REVERSAL_MIN_CHANGE_PCT
            )
        scan_change_pct = 0.0 if same_quote else current_change_pct - previous_change_pct

        state = {
            "last_time": quote_time,
            "last_price": price,
            "last_change_pct": current_change_pct,
            "previous_change_pct": previous_change_pct,
            "scan_change_pct": round(scan_change_pct, 3),
            "min_change_pct": round(min_change_pct, 3),
            "observed_min_change_pct": round(observed_min_change_pct, 3),
            "low_change_pct": round(low_change_pct, 3),
            "was_underwater": was_underwater,
            "crossed_from_underwater": crossed_from_underwater,
            "observation_count": observation_count,
        }
        self._intraday_reversal_state[code] = state
        return dict(state)

    def _is_priority_watchlist_code(self, code: str) -> bool:
        return code in self.low_base_watchlist_codes or code in self.dynamic_trend_pool

    def _should_scan_capital(
        self,
        current_main_inflow: float,
        current_main_inflow_pct: float,
        recent_funds: list[dict],
        volume_ratio: float,
        change_pct: float,
    ) -> bool:
        """是否需要进入资金异动检测.

        目标:
        - 以 FundFlow 为主，不再依赖 stock_spot.main_net_inflow 的失效硬门槛
        - 保留少量必要的预筛，避免把所有股票都交给资金检测器
        """
        abs_inflow = abs(current_main_inflow)
        abs_inflow_pct = abs(current_main_inflow_pct)

        if abs_inflow >= self.capital_detector.MAIN_INFLOW_CRITICAL:
            return True
        if abs_inflow >= self.capital_detector.MAIN_INFLOW_MAJOR and abs_inflow_pct >= 5:
            return True
        if abs_inflow >= self.capital_detector.MAIN_INFLOW_MINOR and volume_ratio >= 1.8 and abs(change_pct) >= 2.5:
            return True

        if recent_funds and len(recent_funds) >= 3:
            recent_values = [float(item.get("main_net_inflow", 0) or 0) for item in recent_funds[:5]]
            positive_days = sum(1 for value in recent_values[:3] if value > 0)
            negative_days = sum(1 for value in recent_values[:3] if value < 0)
            recent_total = sum(recent_values[:3])
            if positive_days >= 3 or negative_days >= 3:
                return True
            if abs(recent_total) >= self.capital_detector.MAIN_INFLOW_CRITICAL:
                return True

        # 量价本身不再单独构成“资金异动”，但若同时伴随明显放量与涨跌，可放行做次级确认
        if abs_inflow >= self.capital_detector.MAIN_INFLOW_MINOR and volume_ratio >= self.capital_detector.VOLUME_RATIO_EXTREME and abs(change_pct) >= 4:
            return True

        return False

    def _is_actionable_breakthrough(
        self,
        signal: BreakthroughSignal,
        change_pct: float,
        min5_change: float,
        volume_ratio: float,
    ) -> bool:
        """过滤突破泛化，只保留更像盘中异动的突破."""
        if signal.is_false_breakout:
            return False

        if signal.signal_type == "price_high":
            return signal.score >= 60 or volume_ratio >= 1.3 or change_pct >= 2.0

        if signal.signal_type == "momentum_burst":
            return signal.score >= 68 and (min5_change >= 1.0 or (change_pct >= 2.5 and volume_ratio >= 1.3))

        if signal.signal_type == "bollinger":
            return signal.score >= 70 and (volume_ratio >= 1.3 or min5_change >= 0.8)

        if signal.signal_type == "ma":
            ma_name = str(signal.detail.get("ma") or "")
            # 均线多头排列/站上MA5/MA10 更适合作为趋势就绪，不直接进入实时异动流
            if signal.description == "均线多头排列":
                return False
            if ma_name not in {"MA20", "MA60"}:
                return False
            return signal.score >= 75 and volume_ratio >= 1.5 and (change_pct >= 2.0 or min5_change >= 1.0)

        return False

    def _calc_momentum_burst_score(
        self,
        *,
        change_pct: float,
        min5_change: float,
        volume_ratio: float,
        support_strength: float,
        near_high_ratio: float,
    ) -> float:
        score = 62.0
        score += min(max(change_pct - 2.0, 0.0), 4.0) * 3.0
        score += min(max(min5_change - 0.8, 0.0), 2.0) * 6.0
        score += min(max(volume_ratio - 1.2, 0.0), 2.5) * 4.0
        score += min(max(support_strength - 50.0, 0.0), 40.0) * 0.15
        score += min(max((near_high_ratio - 0.992) * 1000, 0.0), 8.0)
        return min(score, 88.0)

    def _detect_positive_acceleration_setup(
        self,
        *,
        spot: StockSpot,
        current_fund: dict,
        sector_context: dict,
        board_context: dict,
        reversal_state: dict | None,
        today: date,
        fund_snapshot_is_stale: bool,
        fund_source: str,
        tech: dict | None = None,
    ) -> tuple[float, str, dict] | None:
        """识别红盘放量加速，并捕捉单轮快照跨过常规窗口的爆拉路径。"""
        price = _safe_float(spot.price)
        high_price = _safe_float(spot.high)
        avg_price = _safe_float(spot.avg_price)
        limit_up = _safe_float(spot.limit_up)
        if min(price, high_price) <= 0:
            return None

        change_pct = _safe_float(spot.change_pct)
        min5_change = _spot_effective_min5_change(spot)
        scan_change_pct = _safe_float((reversal_state or {}).get("scan_change_pct"))
        previous_change_pct = _safe_float(
            (reversal_state or {}).get("previous_change_pct"),
            change_pct,
        )
        amount_flow = dict(getattr(spot, "_intraday_amount_flow", {}) or {})
        rolling_60s = dict(getattr(spot, "_rolling_60s_momentum", {}) or {})
        rolling_60s_change_pct = _safe_float(
            rolling_60s.get("rolling_60s_change_pct")
        )
        rolling_60s_confirmed = bool(rolling_60s.get("rolling_60s_confirmed"))
        rolling_60s_tier = str(rolling_60s.get("rolling_60s_tier") or "")
        # 价格动能必须与用来确认的增量成交处在同一窗口。
        # min5只作预筛/展示，不再与最近一轮成交速率拼接成买点。
        acceleration_pct = (
            rolling_60s_change_pct if rolling_60s_confirmed else scan_change_pct
        )
        intraday_amount_confirmed = bool(
            amount_flow.get("intraday_amount_confirmed")
        )
        volume_ratio = _safe_float(spot.volume_ratio, 1.0)
        turnover = _safe_float(spot.turnover)
        amplitude = _safe_float(spot.amplitude)
        near_high_ratio = price / high_price
        price_vs_avg_pct = (
            (price / avg_price - 1.0) * 100.0 if avg_price > 0 else -99.0
        )
        catchup_acceleration = bool(
            settings.ANOMALY_POSITIVE_ACCELERATION_MAX_CHANGE_PCT
            < change_pct
            <= settings.ANOMALY_POSITIVE_ACCELERATION_CATCHUP_MAX_CHANGE_PCT
            and previous_change_pct
            <= settings.ANOMALY_POSITIVE_ACCELERATION_MAX_CHANGE_PCT
            and scan_change_pct
            >= settings.ANOMALY_POSITIVE_ACCELERATION_CATCHUP_MIN_MOMENTUM_PCT
        )
        pre_limit_up_acceleration = bool(
            limit_up > 0
            and limit_up * settings.ANOMALY_POSITIVE_ACCELERATION_PRE_LIMIT_MIN_PRICE_RATIO
            <= price
            < limit_up * settings.ANOMALY_POSITIVE_ACCELERATION_PRE_LIMIT_MAX_PRICE_RATIO
            and change_pct
            <= settings.ANOMALY_POSITIVE_ACCELERATION_LIMIT_UP_MAX_CHANGE_PCT
            and previous_change_pct
            <= settings.ANOMALY_POSITIVE_ACCELERATION_CATCHUP_MAX_CHANGE_PCT
            and acceleration_pct
            >= settings.ANOMALY_POSITIVE_ACCELERATION_PRE_LIMIT_MIN_MOMENTUM_PCT
        )
        limit_up_acceleration = bool(
            limit_up > 0
            and price >= limit_up * 0.995
            and change_pct
            <= settings.ANOMALY_POSITIVE_ACCELERATION_LIMIT_UP_MAX_CHANGE_PCT
            and previous_change_pct
            <= settings.ANOMALY_POSITIVE_ACCELERATION_CATCHUP_MAX_CHANGE_PCT
            and scan_change_pct
            >= settings.ANOMALY_POSITIVE_ACCELERATION_LIMIT_UP_MIN_MOMENTUM_PCT
        )
        if (
            limit_up > 0
            and price >= limit_up * 0.985
            and not (pre_limit_up_acceleration or limit_up_acceleration)
        ):
            return None
        regular_acceleration = bool(
            settings.ANOMALY_POSITIVE_ACCELERATION_MIN_CHANGE_PCT
            <= change_pct
            <= settings.ANOMALY_POSITIVE_ACCELERATION_MAX_CHANGE_PCT
        )
        rolling_60s_acceleration = bool(
            rolling_60s_confirmed
            and settings.ANOMALY_UNDERWATER_ACCELERATION_MIN_CHANGE_PCT
            <= change_pct
            <= settings.ANOMALY_POSITIVE_ACCELERATION_MAX_CHANGE_PCT
        )
        max_amplitude = (
            settings.ANOMALY_POSITIVE_ACCELERATION_CATCHUP_MAX_AMPLITUDE
            if catchup_acceleration or pre_limit_up_acceleration or limit_up_acceleration
            else 9.0
        )
        max_turnover = (
            settings.ANOMALY_POSITIVE_ACCELERATION_CATCHUP_MAX_TURNOVER
            if catchup_acceleration or pre_limit_up_acceleration or limit_up_acceleration
            else 18.0
        )
        if limit_up_acceleration:
            min_momentum = (
                settings.ANOMALY_POSITIVE_ACCELERATION_LIMIT_UP_MIN_MOMENTUM_PCT
            )
        elif pre_limit_up_acceleration:
            min_momentum = (
                settings.ANOMALY_POSITIVE_ACCELERATION_PRE_LIMIT_MIN_MOMENTUM_PCT
            )
        elif catchup_acceleration:
            min_momentum = (
                settings.ANOMALY_POSITIVE_ACCELERATION_CATCHUP_MIN_MOMENTUM_PCT
            )
        else:
            min_momentum = (
                settings.ANOMALY_ROLLING_60S_MIN_CHANGE_PCT
                if rolling_60s_acceleration
                else settings.ANOMALY_POSITIVE_ACCELERATION_MIN_MOMENTUM_PCT
            )
        min_volume_ratio = (
            settings.ANOMALY_ROLLING_60S_MIN_VOLUME_RATIO
            if rolling_60s_acceleration
            else settings.ANOMALY_POSITIVE_ACCELERATION_MIN_VOLUME_RATIO
        )
        max_volume_ratio = (
            settings.ANOMALY_ROLLING_60S_MAX_VOLUME_RATIO
            if rolling_60s_acceleration
            else settings.ANOMALY_POSITIVE_ACCELERATION_MAX_VOLUME_RATIO
        )
        min_price_vs_avg_pct = -0.8 if rolling_60s_acceleration else 0.0
        near_high_ready = bool(
            rolling_60s.get("rolling_60s_path_confirmed")
            if rolling_60s_acceleration
            else near_high_ratio
            >= settings.ANOMALY_POSITIVE_ACCELERATION_MIN_NEAR_HIGH_RATIO
        )
        price_amount_window_confirmed = bool(
            rolling_60s_confirmed
            or (
                intraday_amount_confirmed
                and scan_change_pct >= min_momentum
            )
        )
        if not (
            (
                regular_acceleration
                or catchup_acceleration
                or pre_limit_up_acceleration
                or limit_up_acceleration
                or rolling_60s_acceleration
            )
            and acceleration_pct >= min_momentum
            and min_volume_ratio <= volume_ratio
            <= max_volume_ratio
            and near_high_ready
            and price_vs_avg_pct >= min_price_vs_avg_pct
            and amplitude <= max_amplitude
            and turnover <= max_turnover
            and price_amount_window_confirmed
        ):
            return None
        if self._has_acceleration_distribution_risk(spot):
            return None

        support_strength = _safe_float(getattr(spot, "support_strength_score", 0))
        imbalance = _safe_float(getattr(spot, "orderbook_imbalance", 0))
        bid_depth_5 = _safe_float(getattr(spot, "bid_depth_5", 0))
        ask_depth_5 = _safe_float(getattr(spot, "ask_depth_5", 0))
        current_main_inflow = _safe_float(current_fund.get("main_net_inflow"))
        current_main_inflow_pct = _safe_float(current_fund.get("main_net_inflow_pct"))
        current_fund_source = str(current_fund.get("source") or fund_source or "")
        fund_is_stale = bool(current_fund.get("is_stale")) or bool(fund_snapshot_is_stale)
        fund_fresh = bool(
            current_fund
            and current_fund_source in {"eastmoney_main_fund", "fund_flow"}
            and not fund_is_stale
        )
        orderbook_confirmed = bool(
            support_strength >= 60
            and imbalance >= 0.05
            and bid_depth_5 > ask_depth_5 * 1.03
        )
        fund_confirmed = bool(
            fund_fresh
            and current_main_inflow > 0
            and (current_main_inflow_pct >= 2.0 or current_main_inflow >= 5e7)
        )
        sector_confirmed = self._has_positive_sector_driver(sector_context)
        leader_first_move = bool(
            support_strength >= 78
            and imbalance >= 0.35
            and volume_ratio >= 1.3
            and acceleration_pct >= 1.0
        )
        # leader_first_move本质上是更强的盘口确认，不能和
        # orderbook_confirmed重复计数，否则一项事实会被误算成两项共振。
        core_confirmation_count = sum(
            (orderbook_confirmed, fund_confirmed, sector_confirmed)
        )
        priority_pool_member = self._is_priority_watchlist_code(spot.code)
        strong_tape_confirmed = bool(
            (rolling_60s_confirmed and rolling_60s_tier == "strong")
            or pre_limit_up_acceleration
            or limit_up_acceleration
            or (
                catchup_acceleration
                and acceleration_pct
                >= settings.ANOMALY_POSITIVE_ACCELERATION_CATCHUP_MIN_MOMENTUM_PCT
            )
        )
        # 东财主力净额与腾讯盘口不是同一采样时刻。直线拉升初段，资金快照
        # 可能仍停留在拉升前的净流出状态；若把它作为 OR 硬否决，会同时漏掉
        # “盘前形态池强票”和“板块共振强票”。这里只允许同窗>=1%强急拉、
        # 买盘承接明确，且形态池/板块至少命中一项时穿透；信号仍只作异动观察。
        fund_flow_divergence = bool(
            fund_fresh
            and (
                current_main_inflow <= -1e8
                or current_main_inflow_pct <= -4.0
            )
        )
        fund_flow_divergence_overridden = bool(
            fund_flow_divergence
            and strong_tape_confirmed
            and orderbook_confirmed
            and (priority_pool_member or sector_confirmed or leader_first_move)
        )
        if fund_flow_divergence and not fund_flow_divergence_overridden:
            return None

        min_core_confirmation_count = (
            1
            if strong_tape_confirmed
            and orderbook_confirmed
            and (priority_pool_member or leader_first_move)
            else 2
        )
        if (
            core_confirmation_count < min_core_confirmation_count
            or not (orderbook_confirmed or fund_confirmed)
        ):
            return None

        signal_label = (
            "极速封板确认"
            if limit_up_acceleration
            else "涨停前放量加速预警"
            if pre_limit_up_acceleration
            else "盘中爆拉追赶预警"
            if catchup_acceleration
            else "60秒放量强急拉"
            if rolling_60s_tier == "strong"
            else "60秒放量急拉预警"
            if rolling_60s_acceleration
            else "红盘放量二次加速预警"
        )
        confirmations = [
            (
                f"连续快照急拉至涨停{change_pct:+.1f}%"
                if limit_up_acceleration
                else f"连续快照进入涨停前强度区{change_pct:+.1f}%"
                if pre_limit_up_acceleration
                else f"连续快照跨越常规窗口至{change_pct:+.1f}%"
                if catchup_acceleration
                else (
                    f"滚动{_safe_float(rolling_60s.get('rolling_60s_interval_sec')):.0f}秒"
                    f"累计上涨{rolling_60s_change_pct:+.2f}%"
                )
                if rolling_60s_acceleration
                else f"红盘{change_pct:+.1f}%仍在二次加速窗口"
            ),
            (
                f"短周期加速{acceleration_pct:+.1f}% / 成交速率"
                f"{_safe_float(rolling_60s.get('rolling_60s_amount_pace_ratio')):.1f}倍"
                if rolling_60s_acceleration
                else f"短周期加速{acceleration_pct:+.1f}% / "
                f"成交速率{_safe_float(amount_flow.get('intraday_amount_pace_ratio')):.1f}倍"
            ),
            (
                "拉升末端保持在60秒窗口高位且站稳VWAP附近"
                if rolling_60s_acceleration
                else "价格贴近日内高点且站稳VWAP附近"
            ),
        ]
        if scan_change_pct >= settings.ANOMALY_POSITIVE_ACCELERATION_MIN_MOMENTUM_PCT:
            confirmations.append(
                f"连续快照从{_safe_float((reversal_state or {}).get('previous_change_pct')):+.1f}%加速"
            )
        if orderbook_confirmed:
            confirmations.append(f"盘口承接{support_strength:.0f}且买盘占优")
        if fund_confirmed:
            confirmations.append(f"主力净流入占比{current_main_inflow_pct:.1f}%")
        if sector_confirmed:
            confirmations.append("主驱动板块同步转强")
        if leader_first_move:
            confirmations.append("强盘口先于板块快速启动")
        if fund_flow_divergence_overridden:
            confirmations.append("资金快照滞后于即时量价，仅按强急拉观察")

        score = self._calc_momentum_burst_score(
            change_pct=change_pct,
            min5_change=acceleration_pct,
            volume_ratio=volume_ratio,
            support_strength=support_strength,
            near_high_ratio=near_high_ratio,
        )
        score += min(core_confirmation_count, 3) * 2.0
        if rolling_60s_tier == "medium":
            score += 2.0
        elif rolling_60s_tier == "strong":
            score += 5.0
        detail = {
            "signal_type": "positive_acceleration",
            "signal_label": signal_label,
            "positive_acceleration_confirmed": True,
            "positive_acceleration_catchup": catchup_acceleration,
            "positive_acceleration_pre_limit_up": pre_limit_up_acceleration,
            "positive_acceleration_limit_up": limit_up_acceleration,
            "positive_acceleration_rolling60": rolling_60s_acceleration,
            # 0.3%档只有资金/盘口/板块至少三项共振才允许轻提醒；0.6%以上
            # 延用两项共振门槛，避免把正常价格噪声推给飞书。
            "rolling_60s_alert_pushable": bool(
                rolling_60s_tier in {"medium", "strong"}
                or (rolling_60s_tier == "watch" and core_confirmation_count >= 3)
            ),
            # 全市场都会检测急拉，但只有盘前形态/重点池成员才能把急拉升级为买点；
            # 其余保留“异动观察”身份，避免把扫描覆盖范围冒充成候选池质量。
            "detection_pool_member": priority_pool_member,
            "market_wide_detection": True,
            "detection_pool_source": (
                "dynamic_positive_acceleration"
                if priority_pool_member
                else "market_wide_positive_acceleration"
            ),
            "quality_score": round(min(score, 92.0), 1),
            "ma_status": (tech or {}).get("ma_status", ""),
            "pullback_probability": "medium",
            "days_near_pressure": 0,
            "is_false_breakout": False,
            "acceleration_confirmations": confirmations[:8],
            "positive_acceleration_core_confirmation_count": core_confirmation_count,
            "positive_acceleration_min_core_confirmation_count": min_core_confirmation_count,
            "fund_flow_divergence": fund_flow_divergence,
            "fund_flow_divergence_overridden": fund_flow_divergence_overridden,
            "scan_change_pct": round(scan_change_pct, 3),
            "previous_change_pct": round(
                previous_change_pct, 3
            ),
            "acceleration_pct": round(acceleration_pct, 3),
            "acceleration_window_source": (
                "rolling_60s" if rolling_60s_acceleration else "scan_interval"
            ),
            **amount_flow,
            **rolling_60s,
            "near_high_ratio": round(near_high_ratio, 4),
            "price_vs_avg_pct": round(price_vs_avg_pct, 2),
            "main_net_inflow": current_main_inflow,
            "main_net_inflow_pct": current_main_inflow_pct,
            **fund_order_breakdown(current_fund),
            "fund_data_degraded": not fund_fresh,
            "source": current_fund_source if fund_fresh else "tencent_realtime",
            "as_of": str(
                current_fund.get("as_of")
                or getattr(spot, "updated_at", "")
                or today
            ),
            "is_stale": False,
            **self._build_quote_detail(spot),
            **self._build_orderbook_detail(spot),
            **sector_context,
            **board_context,
            "breakout_anchor": round(high_price, 3),
        }
        return min(score, 92.0), signal_label, detail

    def _has_positive_sector_driver(self, sector_context: dict) -> bool:
        """主线板块同步转强，用于低吸/突破推送确认。"""
        for item in sector_context.get("sector_factors") or []:
            if (
                _is_causal_trade_driver_sector(item)
                and
                _safe_float(item.get("fund_flow")) > 0
                and _safe_float(item.get("change_pct")) > 0
                and (
                    _safe_float(item.get("strength_score")) >= 45
                    or int(item.get("limit_up_count") or 0) >= 2
                )
            ):
                return True
        return False

    @staticmethod
    def _build_market_repair_context(
        spots: list[StockSpot],
        limit_up_count: int,
    ) -> dict:
        """识别强修复日，避免仅用单股均线判断而漏掉产业链反包。

        这里只使用扫描当时已经可见的实时行情和涨停池，不引入收盘后数据，
        因而盘中推送和历史回放保持同一时间口径。
        """
        changes = [
            _safe_float(getattr(spot, "change_pct", 0))
            for spot in spots
            if _safe_float(getattr(spot, "price", 0)) > 0
            and -20.0 <= _safe_float(getattr(spot, "change_pct", 0)) <= 20.0
        ]
        if not changes:
            return {
                "active": False,
                "up_ratio": 0.0,
                "average_change_pct": 0.0,
                "limit_up_count": int(limit_up_count or 0),
            }

        up_ratio = sum(1 for value in changes if value > 0) / len(changes)
        average_change_pct = sum(changes) / len(changes)
        active = bool(
            up_ratio >= 0.52
            and average_change_pct >= 0.35
            and int(limit_up_count or 0) >= 12
        )
        return {
            "active": active,
            "up_ratio": round(up_ratio, 4),
            "average_change_pct": round(average_change_pct, 3),
            "limit_up_count": int(limit_up_count or 0),
        }

    @staticmethod
    def _resolve_sector_repair_driver(sector_context: dict) -> dict | None:
        """返回具备产业链共振的最强板块，微涨陪跑板块不能触发。"""
        candidates = []
        for item in sector_context.get("sector_factors") or []:
            change_pct = _safe_float(item.get("change_pct"))
            fund_flow = _safe_float(item.get("fund_flow"))
            strength_score = _safe_float(item.get("strength_score"))
            limit_up_count = int(item.get("limit_up_count") or 0)
            peer_count = int(item.get("peer_count") or 0)
            if (
                fund_flow > 0
                and change_pct >= 2.8
                and strength_score >= 75
                and limit_up_count >= 5
                and peer_count >= 3
            ):
                candidates.append(item)
        if not candidates:
            return None
        return max(
            candidates,
            key=lambda item: (
                _safe_float(item.get("change_pct")),
                _safe_float(item.get("strength_score")),
                min(int(item.get("limit_up_count") or 0), 10),
                int(item.get("peer_count") or 0),
            ),
        )

    @staticmethod
    def _resolve_old_hot_repair_driver(sector_context: dict) -> dict | None:
        """旧高标超跌修复要求真实板块带动，弱概念映射不能单独触发。"""
        candidates = []
        for item in sector_context.get("sector_factors") or []:
            if (
                _safe_float(item.get("fund_flow")) > 0
                and _safe_float(item.get("change_pct")) >= 1.0
                and _safe_float(item.get("strength_score")) >= 60
                and (
                    int(item.get("limit_up_count") or 0) >= 2
                    or int(item.get("peer_count") or 0) >= 2
                )
            ):
                candidates.append(item)
        if not candidates:
            return None
        return max(
            candidates,
            key=lambda item: (
                _safe_float(item.get("strength_score")),
                _safe_float(item.get("change_pct")),
                int(item.get("limit_up_count") or 0),
                int(item.get("peer_count") or 0),
            ),
        )

    def _detect_sector_repair_setup(
        self,
        *,
        spot: StockSpot,
        tech: dict,
        sector_context: dict,
        board_context: dict,
        market_repair_context: dict,
        today: date,
    ) -> tuple[float, str, dict] | None:
        """检测强板块中的低位超跌反包启动点。

        该通道只补足原趋势算法的盲区：前一交易日仍在MA20下方，但盘中已
        出现同产业链多股涨停、温和放量、VWAP与日内高点确认。它不会把单股
        脉冲、连板接力或涨幅过高的标的包装成买点。
        """
        code = str(getattr(spot, "code", "") or "")
        if not code.startswith(("00", "60")):
            return None

        driver = self._resolve_sector_repair_driver(sector_context)
        if not driver:
            return None
        sector_super_strong = bool(
            _safe_float(driver.get("change_pct")) >= 3.0
            and _safe_float(driver.get("strength_score")) >= 80
            and int(driver.get("limit_up_count") or 0) >= 5
        )
        if not market_repair_context.get("active") and not sector_super_strong:
            return None

        price = _safe_float(getattr(spot, "price", 0))
        high_price = _safe_float(getattr(spot, "high", 0))
        low_price = _safe_float(getattr(spot, "low", 0))
        avg_price = _safe_float(getattr(spot, "avg_price", 0))
        limit_up = _safe_float(getattr(spot, "limit_up", 0))
        if min(price, high_price, low_price) <= 0:
            return None
        if limit_up > 0 and price >= limit_up * 0.965:
            return None

        change_pct = _safe_float(getattr(spot, "change_pct", 0))
        min5_change = _spot_effective_min5_change(spot)
        volume_ratio = _safe_float(getattr(spot, "volume_ratio", 1.0), 1.0)
        turnover = _safe_float(getattr(spot, "turnover", 0))
        amplitude = _safe_float(getattr(spot, "amplitude", 0))
        amount = _safe_float(getattr(spot, "amount", 0))
        support_strength = _safe_float(getattr(spot, "support_strength_score", 0))
        imbalance = _safe_float(getattr(spot, "orderbook_imbalance", 0))
        bid_depth_5 = _safe_float(getattr(spot, "bid_depth_5", 0))
        ask_depth_5 = _safe_float(getattr(spot, "ask_depth_5", 0))

        if not (2.0 <= change_pct <= 6.2):
            return None
        if not (0.85 <= volume_ratio <= 2.8):
            return None
        if turnover < 0.2 or turnover > 13.0 or amplitude > 7.5:
            return None
        if amount < 1e8:
            return None
        if int(board_context.get("max_recent_consecutive_days") or 0) >= 2:
            return None
        if int(board_context.get("recent_limit_up_hits") or 0) > 1:
            return None

        profile = tech.get("low_base_profile") or {}
        closes = [_safe_float(value) for value in profile.get("closes") or []]
        highs = [_safe_float(value) for value in profile.get("highs") or []]
        lows = [_safe_float(value) for value in profile.get("lows") or []]
        volumes = [_safe_float(value) for value in profile.get("volumes") or []]
        if len(closes) < 60 or len(highs) < 60 or len(lows) < 60:
            return None

        previous_close = closes[-1]
        ma20 = sum(closes[-20:]) / 20.0
        return_5d = (previous_close / closes[-5] - 1.0) * 100.0 if closes[-5] > 0 else 0.0
        return_20d = (previous_close / closes[-20] - 1.0) * 100.0 if closes[-20] > 0 else 0.0
        return_60d = (previous_close / closes[-60] - 1.0) * 100.0 if closes[-60] > 0 else 0.0
        range_low = min(lows[-120:])
        range_high = max(highs[-120:])
        position_120 = (
            (previous_close - range_low) / (range_high - range_low)
            if range_high > range_low
            else 1.0
        )
        volume_dry_up_ratio = 1.0
        if len(volumes) >= 20 and sum(volumes[-20:]) > 0:
            volume_dry_up_ratio = (sum(volumes[-5:]) / 5.0) / (sum(volumes[-20:]) / 20.0)

        # 前一日必须仍属于低位/超跌结构，避免把已在高位的顺势拉升误判为修复。
        if not (-45.0 <= return_20d <= -6.0):
            return None
        if previous_close > ma20 * 0.98 and return_20d > -10.0:
            return None
        if position_120 > 0.72:
            return None
        if not (0.55 <= volume_dry_up_ratio <= 1.45):
            return None

        near_intraday_high = price >= high_price * 0.985
        vwap_reclaimed = avg_price > 0 and price >= avg_price * 0.998
        orderbook_confirmed = bool(
            support_strength >= 58
            and (
                imbalance >= 0.05
                or (bid_depth_5 > 0 and bid_depth_5 > ask_depth_5 * 1.05)
            )
        )
        momentum_confirmed = min5_change >= 0.2
        confirmations = [
            label
            for ready, label in (
                (near_intraday_high, "股价贴近日内高点"),
                (vwap_reclaimed, "重新站稳VWAP"),
                (momentum_confirmed, f"5分钟动能{min5_change:+.1f}%"),
                (orderbook_confirmed, f"盘口承接{support_strength:.0f}"),
            )
            if ready
        ]
        if not (near_intraday_high and vwap_reclaimed and orderbook_confirmed):
            return None

        net_profit_growth = _safe_float(getattr(spot, "net_profit_growth", 0))
        pe_ttm = _safe_float(getattr(spot, "pe_ttm", 0))
        fundamental_confirmed = net_profit_growth >= 8.0 and 0 < pe_ttm <= 180
        if not fundamental_confirmed:
            return None

        score = 74.0
        score += min(int(driver.get("peer_count") or 0), 5) * 0.6
        score += min(max(_safe_float(driver.get("strength_score")) - 75.0, 0.0), 25.0) * 0.12
        score += min(max(_safe_float(driver.get("change_pct")) - 2.8, 0.0), 3.0) * 1.5
        score += 4.0 if 2.5 <= change_pct <= 4.8 else 1.0
        score += 2.0 if 1.0 <= volume_ratio <= 2.2 else 0.0
        score += 2.0 if fundamental_confirmed else 0.0
        score += min(max(support_strength - 58.0, 0.0), 25.0) * 0.16
        score += min(max(imbalance - 0.05, 0.0), 0.4) * 6.0
        score += 2.0 if near_intraday_high and vwap_reclaimed else 0.0
        score += 2.0 if -30.0 <= return_20d <= -10.0 else 0.0
        score += min(max(0.35 - position_120, 0.0), 0.35) * 4.0
        score += min(max(np.log10(max(amount / 1e8, 1.0)), 0.0), 2.0)
        quality_score = min(score, 94.0)

        signal_label = "强修复板块低位启动"
        detail = {
            "signal_type": "sector_repair_reversal",
            "signal_label": signal_label,
            "sector_repair_confirmed": True,
            "detection_pool_member": True,
            "watchlist_member": True,
            "watchlist_label": "强修复启动检测池",
            "repair_regime_active": bool(market_repair_context.get("active")),
            "market_repair_context": dict(market_repair_context),
            "sector_repair_driver": dict(driver),
            "sector_repair_confirmations": confirmations,
            "quality_score": round(quality_score, 1),
            "ma_status": "repair_reversal",
            "pullback_probability": "medium",
            "days_near_pressure": 0,
            "is_false_breakout": False,
            "near_intraday_high": near_intraday_high,
            "vwap_reclaimed": vwap_reclaimed,
            "orderbook_confirmed": orderbook_confirmed,
            "fundamental_confirmed": fundamental_confirmed,
            "previous_close_to_ma20": round(previous_close / ma20, 4),
            "return_5d": round(return_5d, 2),
            "return_20d": round(return_20d, 2),
            "return_60d": round(return_60d, 2),
            "position_120": round(position_120, 4),
            "volume_dry_up_ratio": round(volume_dry_up_ratio, 3),
            "repair_support": round(max(low_price, avg_price * 0.99 if avg_price > 0 else low_price), 2),
            "breakout_anchor": round(high_price, 3),
            "source": "stock_spot",
            "as_of": str(getattr(spot, "updated_at", "") or datetime.now().isoformat()),
            **self._build_quote_detail(spot),
            **self._build_orderbook_detail(spot),
            **sector_context,
            **board_context,
        }
        return quality_score, signal_label, detail

    def _detect_old_hot_oversold_repair_setup(
        self,
        *,
        spot: StockSpot,
        tech: dict,
        sector_context: dict,
        board_context: dict,
        today: date,
    ) -> tuple[float, str, dict] | None:
        """检测天娱数科式旧高标深跌后的首日强修复。

        历史连板记忆、充分回撤、缩量沉淀和当日板块/盘口确认必须同时存在；
        该通道只产生 A2 候选，不把超跌反抽伪装成已经形成的趋势。
        """
        code = str(getattr(spot, "code", "") or "")
        if not code.startswith(("00", "60")):
            return None

        driver = self._resolve_old_hot_repair_driver(sector_context)
        if not driver:
            return None

        price = _safe_float(getattr(spot, "price", 0))
        prev_close = _safe_float(getattr(spot, "prev_close", 0))
        high_price = _safe_float(getattr(spot, "high", 0))
        low_price = _safe_float(getattr(spot, "low", 0))
        avg_price = _safe_float(getattr(spot, "avg_price", 0))
        limit_up = _safe_float(getattr(spot, "limit_up", 0))
        if min(price, prev_close, high_price, low_price) <= 0:
            return None
        if limit_up > 0 and price >= limit_up * 0.99:
            return None

        change_pct = _safe_float(getattr(spot, "change_pct", 0))
        min5_change = _spot_effective_min5_change(spot)
        volume_ratio = _safe_float(getattr(spot, "volume_ratio", 1.0), 1.0)
        turnover = _safe_float(getattr(spot, "turnover", 0))
        amplitude = _safe_float(getattr(spot, "amplitude", 0))
        amount = _safe_float(getattr(spot, "amount", 0))
        support_strength = _safe_float(getattr(spot, "support_strength_score", 0))
        imbalance = _safe_float(getattr(spot, "orderbook_imbalance", 0))
        bid_depth_5 = _safe_float(getattr(spot, "bid_depth_5", 0))
        ask_depth_5 = _safe_float(getattr(spot, "ask_depth_5", 0))

        if not (3.5 <= change_pct <= 9.3):
            return None
        if not (0.8 <= volume_ratio <= 2.6):
            return None
        if not (4.0 <= turnover <= 18.0) or amplitude > 12.0 or amount < 1.5e8:
            return None
        if int(board_context.get("recent_limit_up_hits") or 0) > 1:
            return None

        profile = tech.get("low_base_profile") or {}
        closes = [_safe_float(value) for value in profile.get("closes") or []]
        highs = [_safe_float(value) for value in profile.get("highs") or []]
        lows = [_safe_float(value) for value in profile.get("lows") or []]
        volumes = [_safe_float(value) for value in profile.get("volumes") or []]
        if len(closes) < 60 or len(highs) < 60 or len(lows) < 60:
            return None

        previous_close = closes[-1]
        return_20d = (previous_close / closes[-20] - 1.0) * 100.0 if closes[-20] > 0 else 0.0
        range_low = min(lows[-120:])
        range_high = max(highs[-120:])
        position_120 = (
            (previous_close - range_low) / (range_high - range_low)
            if range_high > range_low
            else 1.0
        )
        volume_dry_up_ratio = 1.0
        if len(volumes) >= 20 and sum(volumes[-20:]) > 0:
            volume_dry_up_ratio = (sum(volumes[-5:]) / 5.0) / (sum(volumes[-20:]) / 20.0)

        board_like_indexes = [
            index
            for index in range(1, len(closes))
            if closes[index - 1] > 0
            and (closes[index] / closes[index - 1] - 1.0) * 100.0 >= 8.8
        ]
        max_board_streak = 0
        current_streak = 0
        previous_index = None
        for index in board_like_indexes:
            current_streak = current_streak + 1 if previous_index is not None and index == previous_index + 1 else 1
            max_board_streak = max(max_board_streak, current_streak)
            previous_index = index
        last_board_age = len(closes) - 1 - board_like_indexes[-1] if board_like_indexes else 999

        if len(board_like_indexes) < 2 and max_board_streak < 2:
            return None
        if last_board_age < 8:
            return None
        if not (-55.0 <= return_20d <= -15.0) or position_120 > 0.45:
            return None
        if not (0.5 <= volume_dry_up_ratio <= 1.35):
            return None

        # 对涨幅/振幅越过常规阈值的首日强修复不再一刀切，但必须同时满足
        # 强产业链、深回撤和低位置，避免把普通冲高或高位追涨放进买点通道。
        is_strong_extension = change_pct > 8.6 or amplitude > 9.5 or volume_ratio > 2.4
        if is_strong_extension and not (
            _safe_float(driver.get("strength_score")) >= 80
            and _safe_float(driver.get("change_pct")) >= 2.5
            and int(driver.get("limit_up_count") or 0) >= 5
            and return_20d <= -20.0
            and position_120 <= 0.30
        ):
            return None

        near_intraday_high = price >= high_price * 0.99
        vwap_reclaimed = avg_price > 0 and price >= avg_price * 0.998
        orderbook_confirmed = bool(
            support_strength >= 60
            and (
                imbalance >= 0.05
                or (bid_depth_5 > 0 and bid_depth_5 > ask_depth_5 * 1.05)
            )
        )
        momentum_confirmed = min5_change >= 0.35
        if not (
            near_intraday_high
            and vwap_reclaimed
            and (orderbook_confirmed or momentum_confirmed)
        ):
            return None

        confirmations = [
            label
            for ready, label in (
                (near_intraday_high, "强修复后贴近日内高点"),
                (vwap_reclaimed, "站稳VWAP"),
                (momentum_confirmed, f"5分钟动能{min5_change:+.1f}%"),
                (orderbook_confirmed, f"盘口承接{support_strength:.0f}"),
            )
            if ready
        ]
        score = 72.0
        score += min(max(max_board_streak, 2), 4) * 2.0
        score += 4.0 if len(board_like_indexes) >= 3 else 2.0
        score += 4.0 if return_20d <= -20.0 else 2.0
        score += 3.0 if position_120 <= 0.30 else 1.0
        score += 3.0 if _safe_float(driver.get("strength_score")) >= 70 else 1.0
        score += 3.0 if orderbook_confirmed else 1.0
        score += 2.0 if near_intraday_high and vwap_reclaimed else 0.0
        quality_score = min(score, 94.0)

        signal_label = "旧高标超跌首日强修复"
        detail = {
            "signal_type": "old_hot_oversold_repair",
            "signal_label": signal_label,
            "old_hot_repair_confirmed": True,
            "detection_pool_member": True,
            "watchlist_member": True,
            "watchlist_label": "旧高标超跌修复检测池",
            "quality_score": round(quality_score, 1),
            "ma_status": "repair_reversal",
            "pullback_probability": "medium",
            "days_near_pressure": 0,
            "is_false_breakout": False,
            "near_intraday_high": near_intraday_high,
            "vwap_reclaimed": vwap_reclaimed,
            "orderbook_confirmed": orderbook_confirmed,
            "old_hot_repair_driver": dict(driver),
            "old_hot_repair_confirmations": confirmations,
            "strong_extension_confirmed": is_strong_extension,
            "historical_board_like_count": len(board_like_indexes),
            "historical_max_board_streak": max_board_streak,
            "last_board_age": last_board_age,
            "return_20d": round(return_20d, 2),
            "position_120": round(position_120, 4),
            "volume_dry_up_ratio": round(volume_dry_up_ratio, 3),
            "repair_support": round(max(low_price, avg_price * 0.99 if avg_price > 0 else low_price), 2),
            "breakout_anchor": round(high_price, 3),
            "source": "stock_spot",
            "as_of": str(getattr(spot, "updated_at", "") or datetime.now().isoformat()),
            **self._build_quote_detail(spot),
            **self._build_orderbook_detail(spot),
            **sector_context,
            **board_context,
        }
        return quality_score, signal_label, detail

    def _detect_low_absorb_setup(
        self,
        *,
        spot: StockSpot,
        tech: dict,
        current_fund: dict,
        sector_context: dict,
        board_context: dict,
        today: date,
        latest_fund_trade_date: date | None,
        fund_snapshot_is_stale: bool,
        fund_source: str,
    ) -> tuple[float, str, dict] | None:
        """识别绿盘弱转强/上升通道回踩MA5的低吸买点。

        设计意图:
        - 不追高: 涨幅、量比、振幅、换手必须在健康区间。
        - 要确认: 盘口承接、VWAP/MA5修复、资金或板块至少形成两项确认。
        """
        price = _safe_float(spot.price)
        prev_close = _safe_float(spot.prev_close)
        open_price = _safe_float(spot.open)
        high_price = _safe_float(spot.high)
        low_price = _safe_float(spot.low)
        avg_price = _safe_float(spot.avg_price)
        limit_up = _safe_float(spot.limit_up)
        limit_down = _safe_float(spot.limit_down)
        if price <= 0 or prev_close <= 0 or low_price <= 0:
            return None
        if limit_up > 0 and price >= limit_up * 0.985:
            return None
        if limit_down > 0 and price <= limit_down * 1.015:
            return None

        change_pct = _safe_float(spot.change_pct)
        min5_change = _spot_effective_min5_change(spot)
        volume_ratio = _safe_float(spot.volume_ratio, 1.0)
        amplitude = _safe_float(spot.amplitude)
        turnover = _safe_float(spot.turnover)
        support_strength = _safe_float(getattr(spot, "support_strength_score", 0))
        imbalance = _safe_float(getattr(spot, "orderbook_imbalance", 0))
        bid_depth_5 = _safe_float(getattr(spot, "bid_depth_5", 0))
        ask_depth_5 = _safe_float(getattr(spot, "ask_depth_5", 0))
        ma5 = _safe_float(tech.get("ma5"))
        ma10 = _safe_float(tech.get("ma10"))
        ma20 = _safe_float(tech.get("ma20"))
        ma20_slope_5d = _safe_float(tech.get("ma20_slope_5d"))
        return_20d = _safe_float(tech.get("return_20d"))

        if change_pct < -3.5 or change_pct > 2.8:
            return None
        if volume_ratio < 0.75 or volume_ratio > 2.6:
            return None
        if amplitude > 6.8 or turnover > 14.5:
            return None
        if ma5 <= 0 or ma10 <= 0 or ma20 <= 0:
            return None
        if "ma20_slope_5d" in tech and ma20_slope_5d < 0:
            return None
        if "return_20d" in tech and not (0.0 <= return_20d <= 25.0):
            return None

        intraday_rebound_pct = (price / low_price - 1.0) * 100.0
        price_vs_avg_pct = (price / avg_price - 1.0) * 100.0 if avg_price > 0 else 0.0
        price_vs_avg_pct = (price / avg_price - 1.0) * 100.0 if avg_price > 0 else 0.0
        distance_to_ma5_pct = (price / ma5 - 1.0) * 100.0
        ma_stack_ready = ma5 >= ma10 * 0.995 and ma10 >= ma20 * 0.995 and price >= ma10 * 0.995
        ma5_pullback = (
            ma_stack_ready
            and -1.2 <= distance_to_ma5_pct <= 2.4
            and low_price <= ma5 * 1.015
            and price >= ma5 * 0.995
        )
        green_reversal = (
            -3.2 <= change_pct <= 1.2
            and min5_change >= 0.35
            and low_price <= prev_close * 0.992
            and intraday_rebound_pct >= 0.8
            and (
                (avg_price > 0 and price >= avg_price * 0.998)
                or (open_price > 0 and price >= open_price * 0.998)
            )
        )
        if not ma5_pullback and not green_reversal:
            return None

        current_main_inflow = _safe_float(current_fund.get("main_net_inflow"))
        current_main_inflow_pct = _safe_float(current_fund.get("main_net_inflow_pct"))
        has_positive_sector = self._has_positive_sector_driver(sector_context)
        heavy_outflow = current_main_inflow <= -1e8 or current_main_inflow_pct <= -4.0
        if heavy_outflow:
            return None
        orderbook_confirmed = support_strength >= 60 and imbalance >= 0.05 and bid_depth_5 > ask_depth_5 * 1.05
        fund_confirmed = current_main_inflow > 0 and current_main_inflow_pct >= 3.0
        vwap_reclaimed = avg_price > 0 and price >= avg_price * 0.998

        confirmations = []
        if ma5_pullback:
            confirmations.append("上升通道回踩MA5不破")
        if green_reversal:
            confirmations.append("绿盘下探后5分钟转强")
        if orderbook_confirmed:
            confirmations.append(f"盘口承接{support_strength:.0f}且买盘占优")
        if fund_confirmed:
            confirmations.append(f"主力净流入占比{current_main_inflow_pct:.1f}%")
        if has_positive_sector:
            confirmations.append("主驱动板块同步转强")
        if vwap_reclaimed:
            confirmations.append("重新站回VWAP附近")
        if min5_change >= 0.5:
            confirmations.append(f"5分钟动能{min5_change:+.1f}%")

        if len(confirmations) < 3:
            return None

        score = 58.0
        if ma5_pullback:
            score += 8.0
        if green_reversal:
            score += 8.0
        score += min(max(support_strength - 55.0, 0.0), 25.0) * 0.35
        score += min(max(intraday_rebound_pct - 0.8, 0.0), 2.5) * 3.0
        score += min(max(min5_change - 0.35, 0.0), 1.8) * 4.0
        score += min(max(current_main_inflow_pct - 3.0, 0.0), 6.0) * 1.2
        if current_main_inflow >= 1.5e8:
            score += 4.0
        elif current_main_inflow > 0:
            score += 2.0
        if has_positive_sector:
            score += 5.0
        if vwap_reclaimed:
            score += 3.0
        if volume_ratio <= 1.8 and amplitude <= 5.2:
            score += 3.0

        labels = []
        if green_reversal:
            labels.append("绿盘弱转强低吸")
        if ma5_pullback:
            labels.append("回踩MA5低吸")
        signal_label = " + ".join(labels) or "低吸弱转强"

        current_fund_source = str(current_fund.get("source") or fund_source or "")
        current_fund_is_stale = bool(current_fund.get("is_stale")) or bool(fund_snapshot_is_stale)
        detail = {
            "signal_type": "low_absorb",
            "low_absorb_type": (
                "green_reversal_ma5_pullback"
                if green_reversal and ma5_pullback
                else "green_reversal"
                if green_reversal
                else "ma5_pullback"
            ),
            "signal_label": signal_label,
            "low_absorb_confirmations": confirmations[:6],
            "intraday_rebound_pct": round(intraday_rebound_pct, 2),
            "price_vs_avg_pct": round(price_vs_avg_pct, 2),
            "distance_to_ma5_pct": round(distance_to_ma5_pct, 2),
            "ma5": ma5,
            "ma10": ma10,
            "ma20": ma20,
            "ma20_slope_5d": ma20_slope_5d,
            "return_20d": return_20d,
            "main_net_inflow": current_main_inflow,
            "main_net_inflow_pct": current_main_inflow_pct,
            **fund_order_breakdown(current_fund),
            "source": current_fund_source if current_fund_source else "stock_spot",
            "as_of": str(current_fund.get("as_of") or (str(latest_fund_trade_date) if latest_fund_trade_date else str(today))),
            "is_stale": bool(current_fund_source and current_fund_is_stale),
            **self._build_quote_detail(spot),
            **self._build_orderbook_detail(spot),
            **sector_context,
            **board_context,
        }
        return min(score, 88.0), signal_label, detail

    def _detect_underwater_reversal_setup(
        self,
        *,
        spot: StockSpot,
        current_fund: dict,
        sector_context: dict,
        board_context: dict,
        reversal_state: dict | None,
        today: date,
        fund_snapshot_is_stale: bool,
        fund_source: str,
    ) -> tuple[float, str, dict] | None:
        """识别水下放量急拉预警，以及翻红后的确认买点。"""
        price = _safe_float(spot.price)
        prev_close = _safe_float(spot.prev_close)
        open_price = _safe_float(spot.open)
        high_price = _safe_float(spot.high)
        low_price = _safe_float(spot.low)
        avg_price = _safe_float(spot.avg_price)
        limit_up = _safe_float(spot.limit_up)
        if min(price, prev_close, open_price, high_price, low_price) <= 0:
            return None
        if limit_up > 0 and price >= limit_up * 0.985:
            return None

        change_pct = _safe_float(spot.change_pct)
        min5_change = _spot_effective_min5_change(spot)
        amount_flow = dict(getattr(spot, "_intraday_amount_flow", {}) or {})
        intraday_amount_confirmed = bool(
            amount_flow.get("intraday_amount_confirmed")
        )
        volume_ratio = _safe_float(spot.volume_ratio, 1.0)
        turnover = _safe_float(spot.turnover)
        amplitude = _safe_float(spot.amplitude)
        if amplitude <= 0:
            amplitude = (high_price / low_price - 1.0) * 100.0
        support_strength = _safe_float(getattr(spot, "support_strength_score", 0))
        imbalance = _safe_float(getattr(spot, "orderbook_imbalance", 0))
        bid_depth_5 = _safe_float(getattr(spot, "bid_depth_5", 0))
        ask_depth_5 = _safe_float(getattr(spot, "ask_depth_5", 0))

        low_drop_pct = (low_price / prev_close - 1.0) * 100.0
        intraday_rebound_pct = (price / low_price - 1.0) * 100.0
        price_vs_avg_pct = (price / avg_price - 1.0) * 100.0 if avg_price > 0 else -99.0
        close_position = (price - low_price) / (high_price - low_price) if high_price > low_price else 1.0
        pullback_from_high_pct = (price / high_price - 1.0) * 100.0

        if low_drop_pct > -settings.ANOMALY_UNDERWATER_REVERSAL_MIN_LOW_DROP_PCT:
            return None
        is_underwater_acceleration = bool(
            settings.ANOMALY_UNDERWATER_ACCELERATION_MIN_CHANGE_PCT
            <= change_pct
            < settings.ANOMALY_UNDERWATER_REVERSAL_MIN_CHANGE_PCT
        )
        is_reversal_confirmed = bool(
            settings.ANOMALY_UNDERWATER_REVERSAL_MIN_CHANGE_PCT
            <= change_pct
            <= settings.ANOMALY_UNDERWATER_REVERSAL_MAX_CHANGE_PCT
        )
        if not (is_underwater_acceleration or is_reversal_confirmed):
            return None
        min_price_vs_avg_pct = (
            settings.ANOMALY_UNDERWATER_ACCELERATION_MIN_VWAP_GAP_PCT
            if is_underwater_acceleration
            else 0.0
        )
        if price < open_price or price_vs_avg_pct < min_price_vs_avg_pct:
            return None
        if is_reversal_confirmed and price < prev_close:
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
        if close_position < settings.ANOMALY_UNDERWATER_REVERSAL_MIN_CLOSE_POSITION:
            return None
        if pullback_from_high_pct <= -settings.ANOMALY_UNDERWATER_REVERSAL_MAX_PULLBACK_FROM_HIGH_PCT:
            return None
        if not intraday_amount_confirmed or self._has_acceleration_distribution_risk(spot):
            return None

        state = reversal_state or {}
        scan_change_pct = _safe_float(state.get("scan_change_pct"))
        crossed_from_underwater = bool(state.get("crossed_from_underwater"))
        observation_count = int(state.get("observation_count") or 0)
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
                or crossed_from_underwater
                or (
                    observation_count <= 1
                    and intraday_rebound_pct >= 2.6
                    and close_position >= 0.72
                )
            )
        if not momentum_confirmed:
            return None

        current_main_inflow = _safe_float(current_fund.get("main_net_inflow"))
        current_main_inflow_pct = _safe_float(current_fund.get("main_net_inflow_pct"))
        current_fund_source = str(current_fund.get("source") or fund_source or "")
        current_fund_is_stale = bool(current_fund.get("is_stale")) or bool(fund_snapshot_is_stale)
        fund_fresh = bool(
            current_fund
            and current_fund_source in {"eastmoney_main_fund", "fund_flow"}
            and not current_fund_is_stale
        )
        if fund_fresh and (
            current_main_inflow <= -1e8
            or current_main_inflow_pct <= -4.0
        ):
            return None

        orderbook_confirmed = bool(
            support_strength >= 60
            and imbalance >= 0.05
            and bid_depth_5 > ask_depth_5 * 1.03
        )
        fund_confirmed = bool(
            fund_fresh
            and current_main_inflow > 0
            and (current_main_inflow_pct >= 2.0 or current_main_inflow >= 5e7)
        )
        sector_confirmed = self._has_positive_sector_driver(sector_context)
        leader_first_move = bool(
            support_strength >= 78
            and imbalance >= 0.35
            and volume_ratio >= 1.3
            and (min5_change >= 0.8 or scan_change_pct >= 0.8)
        )
        core_confirmation_count = sum(
            (orderbook_confirmed, fund_confirmed, sector_confirmed, leader_first_move)
        )
        if core_confirmation_count < 2 or not (orderbook_confirmed or fund_confirmed):
            return None

        confirmations = [
            (
                f"仍在水下{change_pct:.1f}%，已从低点急拉{intraday_rebound_pct:.1f}%"
                if is_underwater_acceleration
                else f"日内最低{low_drop_pct:.1f}%后拉回红盘"
            ),
            (
                f"最近成交速率"
                f"{_safe_float(amount_flow.get('intraday_amount_pace_ratio')):.1f}倍，短周期放量加速"
            ),
            (
                f"距离VWAP仅{price_vs_avg_pct:+.1f}%"
                if price_vs_avg_pct < 0
                else f"站回VWAP上方{price_vs_avg_pct:+.1f}%"
            ),
        ]
        if crossed_from_underwater:
            confirmations.append("连续快照确认水下翻红")
        if min5_change >= 0.45:
            confirmations.append(f"5分钟动能{min5_change:+.1f}%")
        elif scan_change_pct >= 0.45:
            confirmations.append(f"单轮扫描加速{scan_change_pct:+.1f}%")
        if orderbook_confirmed:
            confirmations.append(f"盘口承接{support_strength:.0f}且买盘占优")
        if fund_confirmed:
            confirmations.append(f"主力净流入占比{current_main_inflow_pct:.1f}%")
        if sector_confirmed:
            confirmations.append("主驱动板块同步转强")
        if leader_first_move:
            confirmations.append("强盘口先于板块快速启动")

        # 水下预警只给A2，翻红确认后再按资金/盘口/板块质量升级。
        score = 58.0 if is_underwater_acceleration else 62.0
        score += min(max(intraday_rebound_pct - 2.0, 0.0), 4.0) * 2.0
        score += min(max(min5_change, scan_change_pct, 0.0), 2.0) * 4.0
        score += min(max(support_strength - 60.0, 0.0), 25.0) * 0.25
        score += core_confirmation_count * 3.0
        if close_position >= 0.8:
            score += 3.0
        if pullback_from_high_pct >= -0.8:
            score += 2.0

        signal_type = (
            "underwater_acceleration"
            if is_underwater_acceleration
            else "underwater_reversal"
        )
        signal_label = (
            "水下放量急拉预警"
            if is_underwater_acceleration
            else "水下翻红快速启动"
        )
        detail = {
            "signal_type": signal_type,
            "low_absorb_type": signal_type,
            "signal_label": signal_label,
            "underwater_acceleration_confirmed": is_underwater_acceleration,
            "underwater_reversal_confirmed": is_reversal_confirmed,
            "pre_reversal_alert": is_underwater_acceleration,
            "detection_pool_member": self._is_priority_watchlist_code(spot.code),
            "market_wide_detection": True,
            "detection_pool_source": (
                f"dynamic_{signal_type}"
                if self._is_priority_watchlist_code(spot.code)
                else f"market_wide_{signal_type}"
            ),
            "low_absorb_confirmations": confirmations[:8],
            "underwater_core_confirmation_count": core_confirmation_count,
            "low_drop_pct": round(low_drop_pct, 2),
            "intraday_rebound_pct": round(intraday_rebound_pct, 2),
            "price_vs_avg_pct": round(price_vs_avg_pct, 2),
            "close_position": round(close_position, 3),
            "pullback_from_high_pct": round(pullback_from_high_pct, 2),
            "scan_change_pct": round(scan_change_pct, 3),
            **amount_flow,
            "crossed_from_underwater": crossed_from_underwater,
            "fund_data_degraded": not fund_fresh,
            "main_net_inflow": current_main_inflow,
            "main_net_inflow_pct": current_main_inflow_pct,
            **fund_order_breakdown(current_fund),
            "source": current_fund_source if fund_fresh else "tencent_realtime",
            "as_of": str(
                current_fund.get("as_of")
                or getattr(spot, "updated_at", "")
                or today
            ),
            "is_stale": False,
            **self._build_quote_detail(spot),
            **self._build_orderbook_detail(spot),
            **sector_context,
            **board_context,
        }
        return min(score, 94.0), signal_label, detail

    def _detect_trend_driver_watch_setup(
        self,
        *,
        spot: StockSpot,
        current_fund: dict,
        sector_context: dict,
        board_context: dict,
        today: date,
        reversal_state: dict | None = None,
        leader_spot: StockSpot | SimpleNamespace | None = None,
    ) -> tuple[str, float, str, dict] | None:
        """动态趋势池盘中触发：支撑回收低吸或压力位放量突破。"""
        setup = self.dynamic_trend_pool.get(str(spot.code or ""))
        if not setup:
            return None

        price = _safe_float(spot.price)
        prev_close = _safe_float(spot.prev_close)
        open_price = _safe_float(spot.open)
        low_price = _safe_float(spot.low)
        high_price = _safe_float(spot.high)
        avg_price = _safe_float(spot.avg_price)
        limit_up = _safe_float(spot.limit_up)
        if min(price, prev_close, low_price) <= 0:
            return None
        # 已涨停、近两次涨停或明显追高都不再包装成趋势驱动买点。
        if limit_up > 0 and price >= limit_up * 0.985:
            return None
        setup_source = str(setup.get("source") or "")
        setup_stats = dict(setup.get("stats") or {})
        is_pre_board_setup = setup_source.startswith("pre_board_")
        has_long_cycle_profile = bool(setup_stats.get("long_cycle_regime"))
        long_cycle_shape_ready = bool(
            not has_long_cycle_profile
            or (
                setup_stats.get(
                    "pre_board_long_cycle_ready",
                    setup_stats.get("shape_ready"),
                )
                and str(setup_stats.get("long_cycle_regime") or "") != "high_overheat"
                and _safe_float(setup_stats.get("quality_setup_score")) >= 58.0
            )
        )
        is_second_wave_reset_setup = setup_source == "second_wave_reset_watch"
        is_event_relay_setup = setup_source in {
            "event_first_board",
            "event_high_board",
            "second_board_relay",
        }
        is_leader_linkage_setup = setup_source == "leader_linkage_follow"
        is_sector_core_laggard_setup = setup_source == "sector_core_laggard"
        is_main_wave_setup = setup_source in {
            "main_wave_pattern",
            "trend_main_wave_pattern",
            "main_wave_pullback_pattern",
        }
        is_main_wave_shape_pullback = setup_source == "main_wave_pullback_pattern"
        is_tenbagger_pullback_setup = setup_source in {
            "tenbagger_pullback_watch",
            "low_expectation_trend_watch",
        }
        is_low_expectation_setup = setup_source == "low_expectation_trend_watch"
        if (
            int(board_context.get("max_recent_consecutive_days") or 0) >= 2
            and not (
                is_second_wave_reset_setup
                or is_event_relay_setup
                or is_main_wave_setup
            )
        ):
            return None

        support = _safe_float(setup.get("support"))
        resistance = _safe_float(setup.get("resistance"))
        if support <= 0 or resistance <= support:
            return None

        change_pct = _safe_float(spot.change_pct)
        min5_change = _spot_effective_min5_change(spot)
        amount_flow = dict(getattr(spot, "_intraday_amount_flow", {}) or {})
        intraday_amount_confirmed = bool(
            amount_flow.get("intraday_amount_confirmed")
        )
        rolling_60s = dict(getattr(spot, "_rolling_60s_momentum", {}) or {})
        rolling_60s_confirmed = bool(rolling_60s.get("rolling_60s_confirmed"))
        acceleration_distribution_risk = self._has_acceleration_distribution_risk(spot)
        volume_ratio = _safe_float(spot.volume_ratio, 1.0)
        turnover = _safe_float(spot.turnover)
        amplitude = _safe_float(spot.amplitude)
        support_strength = _safe_float(getattr(spot, "support_strength_score", 0))
        imbalance = _safe_float(getattr(spot, "orderbook_imbalance", 0))
        bid_depth_5 = _safe_float(getattr(spot, "bid_depth_5", 0))
        ask_depth_5 = _safe_float(getattr(spot, "ask_depth_5", 0))
        current_inflow = _safe_float(current_fund.get("main_net_inflow"))
        inflow_pct = _safe_float(current_fund.get("main_net_inflow_pct"))
        fund_is_stale = bool(current_fund.get("is_stale"))
        large_order_context = _build_large_order_inflow_context(
            current_fund,
            traded_amount=_safe_float(getattr(spot, "amount", 0)),
        )
        large_order_confirmed = bool(
            large_order_context.get("large_order_inflow_confirmed")
        )
        core_sector_driver = _resolve_main_wave_core_sector_driver(sector_context)
        core_sector_confirmed = core_sector_driver is not None
        sector_leader_confirmed = bool(
            (core_sector_driver or {}).get("is_sector_leader")
        )

        orderbook_confirmed = (
            support_strength >= 65
            and imbalance >= 0.05
            and bid_depth_5 > ask_depth_5 * 1.05
        )
        fund_confirmed = not fund_is_stale and current_inflow > 0 and inflow_pct >= 2.5
        sector_confirmed = self._has_positive_sector_driver(sector_context)
        vwap_reclaimed = avg_price > 0 and price >= avg_price * 0.998
        price_vs_avg_pct = (price / avg_price - 1.0) * 100.0 if avg_price > 0 else 0.0
        near_intraday_high = high_price > 0 and price >= high_price * 0.992
        intraday_rebound_pct = (price / low_price - 1.0) * 100.0
        support_gap_pct = (price / support - 1.0) * 100.0
        breakout_pct = (price / resistance - 1.0) * 100.0

        is_long_base_setup = setup_source == "pre_board_long_base"
        is_momentum_shakeout_setup = setup_source == "pre_board_momentum_shakeout"
        is_probe_wash_setup = setup_source == "pre_board_probe_wash"
        is_probe_breakout_setup = setup_source in {
            "pre_board_probe_wash",
            "pre_board_probe_breakout",
        }
        watchlist_label = (
            "动态低位连板观察池"
            if is_long_base_setup
            else "中期动量缩量洗盘池"
            if is_momentum_shakeout_setup
            else "动态试盘洗盘观察池"
            if is_probe_wash_setup
            else "动态试盘突破观察池"
            if is_probe_breakout_setup
            else "动态强修复观察池"
            if setup_source in {"sector_repair_reversal", "old_hot_oversold_repair"}
            else "重大事件接力观察池"
            if is_event_relay_setup
            else "看A做B联动池"
            if is_leader_linkage_setup
            else "主线核心补涨池"
            if is_sector_core_laggard_setup
            else "高标下杀二波检测池"
            if is_second_wave_reset_setup
            else "牛股潜质回踩池"
            if is_tenbagger_pullback_setup and not is_low_expectation_setup
            else "低位预期趋势确认池"
            if is_low_expectation_setup
            else "动态趋势驱动池"
        )
        common_detail = {
            "watchlist_member": True,
            "watchlist_label": watchlist_label,
            "trend_driver_watchlist": True,
            "pre_board_watchlist": is_pre_board_setup,
            "trend_setup_confirmed": True,
            "trend_setup_label": setup.get("label") or "趋势驱动候选",
            "trend_setup_source": setup_source,
            "trend_setup_state": setup.get("setup_state") or "candidate",
            "trend_setup_score": round(_safe_float(setup.get("score")), 1),
            "long_cycle_regime": str(setup_stats.get("long_cycle_regime") or ""),
            "long_cycle_regime_label": str(setup_stats.get("long_cycle_regime_label") or ""),
            "long_cycle_quality_score": _safe_float(setup_stats.get("quality_setup_score")),
            "long_cycle_shape_ready": long_cycle_shape_ready,
            "pre_board_long_cycle_ready_reason": str(
                setup_stats.get("pre_board_long_cycle_ready_reason") or ""
            ),
            "long_cycle_direct_buy_ready": False,
            "long_cycle_requires_intraday_confirmation": is_pre_board_setup,
            "trend_support": round(support, 2),
            "trend_resistance": round(resistance, 2),
            "support_gap_pct": round(support_gap_pct, 2),
            "breakout_pct": round(breakout_pct, 2),
            "price_vs_avg_pct": round(price_vs_avg_pct, 2),
            "distance_to_ma5_pct": round(support_gap_pct, 2),
            "main_net_inflow": current_inflow,
            "main_net_inflow_pct": inflow_pct,
            **fund_order_breakdown(current_fund),
            **large_order_context,
            "main_wave_core_sector_confirmed": core_sector_confirmed,
            "main_wave_sector_leader_confirmed": sector_leader_confirmed,
            "main_wave_core_sector_driver": core_sector_driver or {},
            "source": str(current_fund.get("source") or "stock_spot"),
            "as_of": str(current_fund.get("as_of") or today),
            "is_stale": fund_is_stale,
            **self._build_quote_detail(spot),
            **self._build_orderbook_detail(spot),
            **sector_context,
            **board_context,
        }

        # 主线先形成涨停扩散时，后排不应等到传统日K突破才入池。这里要求
        # “同一强主线的多重映射 + 滚动60秒真实增量成交 + VWAP附近承接”，
        # 只发A2点火提示，避免把普通概念跟风或无量脉冲当作买点。
        if is_sector_core_laggard_setup:
            expected_sector_code = str(setup_stats.get("sector_code") or "")
            expected_sector_name = str(setup_stats.get("sector_name") or "")
            exact_sector_factor = next(
                (
                    dict(item)
                    for item in (sector_context.get("sector_factors") or [])
                    if _is_main_wave_core_sector_factor(item)
                    and (
                        (
                            expected_sector_code
                            and str(item.get("sector_code") or "") == expected_sector_code
                        )
                        or (
                            expected_sector_name
                            and str(item.get("sector_name") or "") == expected_sector_name
                        )
                    )
                ),
                None,
            )
            rolling_change_pct = _safe_float(
                rolling_60s.get("rolling_60s_change_pct")
            )
            rolling_amount_pace = _safe_float(
                rolling_60s.get("rolling_60s_amount_pace_ratio")
            )
            rolling_increment_ready = bool(
                rolling_60s_confirmed
                and rolling_60s.get("rolling_60s_path_confirmed", True)
                and rolling_change_pct >= settings.ANOMALY_ROLLING_60S_MIN_CHANGE_PCT
                and rolling_amount_pace
                >= settings.ANOMALY_ACCELERATION_MIN_AMOUNT_PACE_RATIO
            )
            price_location_ready = bool(
                price_vs_avg_pct >= -0.2
                and (near_intraday_high or intraday_rebound_pct >= 0.5)
            )
            confirmation_count = sum(
                (
                    exact_sector_factor is not None,
                    orderbook_confirmed,
                    fund_confirmed,
                    large_order_confirmed,
                )
            )
            ignition_ready = bool(
                exact_sector_factor is not None
                and int(setup_stats.get("hot_sector_membership_count") or 0) >= 2
                and int(setup_stats.get("business_aligned_sector_count") or 0) >= 1
                and rolling_increment_ready
                and price_location_ready
                and -2.5 <= change_pct <= 4.8
                and 1.15 <= volume_ratio <= 3.2
                and turnover <= 15.0
                and amplitude <= 8.0
                and not acceleration_distribution_risk
                and confirmation_count >= 2
                and (orderbook_confirmed or fund_confirmed or large_order_confirmed)
            )
            if ignition_ready:
                factor = exact_sector_factor or {}
                confirmations = [
                    (
                        f"{factor.get('sector_name') or expected_sector_name or '主线'}"
                        f"强度{_safe_float(factor.get('strength_score')):.0f} / "
                        f"涨停{int(factor.get('limit_up_count') or 0)}家"
                    ),
                    (
                        f"60秒上涨{rolling_change_pct:+.2f}% / "
                        f"成交速率{rolling_amount_pace:.1f}倍"
                    ),
                    (
                        "站回VWAP"
                        if price_vs_avg_pct >= 0
                        else f"距离VWAP仅{price_vs_avg_pct:+.1f}%"
                    ),
                    f"多重产业映射{int(setup_stats.get('hot_sector_membership_count') or 0)}项",
                    f"主营同行业涨停验证{int(setup_stats.get('business_aligned_sector_count') or 0)}项",
                ]
                if orderbook_confirmed:
                    confirmations.append(f"盘口承接{support_strength:.0f}且买盘占优")
                if fund_confirmed:
                    confirmations.append(f"主力净流入占比{inflow_pct:.1f}%")
                if large_order_confirmed:
                    confirmations.append("大单净流入金额确认")
                score = 80.0
                score += min(max(rolling_change_pct - 0.3, 0.0), 1.2) * 5.0
                score += min(max(volume_ratio - 1.15, 0.0), 1.5) * 2.0
                score += min(max(confirmation_count - 2, 0), 2) * 3.0
                detail = {
                    **common_detail,
                    "signal_type": "sector_core_laggard_ignition",
                    "signal_label": "主线核心补涨点火",
                    "quality_score": round(min(score, 94.0), 1),
                    "sector_core_laggard_confirmed": True,
                    "detection_pool_member": True,
                    "detection_pool_source": "sector_core_laggard",
                    "sector_core_driver": factor,
                    "sector_core_laggard_confirmations": confirmations[:7],
                    "sector_core_confirmation_count": confirmation_count,
                    "near_intraday_high": near_intraday_high,
                    "vwap_reclaimed": vwap_reclaimed,
                    "intraday_rebound_pct": round(intraday_rebound_pct, 2),
                    **rolling_60s,
                }
                return (
                    "breakthrough",
                    min(score, 94.0),
                    "主线核心补涨点火",
                    detail,
                )
            return None

        # 主升浪绿开下杀不是开盘即买：只有低点后重新向上、短周期增量成交
        # 与盘口/资金/板块共同确认时才触发，避免把放量下跌误报为低吸。
        if is_main_wave_setup:
            main_wave_stats = dict(setup.get("stats") or {})
            open_change_pct = (
                (open_price / prev_close - 1.0) * 100.0
                if open_price > 0 and prev_close > 0
                else 0.0
            )
            low_drop_pct = (low_price / prev_close - 1.0) * 100.0
            close_position = (
                (price - low_price) / (high_price - low_price)
                if high_price > low_price
                else 1.0
            )
            pullback_from_high_pct = (
                (price / high_price - 1.0) * 100.0
                if high_price > 0
                else -100.0
            )
            scan_change_pct = _safe_float((reversal_state or {}).get("scan_change_pct"))
            rolling_60s = dict(getattr(spot, "_rolling_60s_momentum", {}) or {})
            rolling_60s_confirmed = bool(rolling_60s.get("rolling_60s_confirmed"))
            short_momentum_pct = max(
                min5_change,
                scan_change_pct,
                (
                    _safe_float(rolling_60s.get("rolling_60s_change_pct"))
                    if rolling_60s_confirmed
                    else 0.0
                ),
            )
            incremental_amount_confirmed = bool(
                intraday_amount_confirmed or rolling_60s_confirmed
            )
            main_wave_memory_ready = bool(
                _safe_float(setup.get("score")) >= 78.0
                and (
                    _safe_float(main_wave_stats.get("pct_12")) >= 18.0
                    or _safe_float(main_wave_stats.get("pct_20")) >= 25.0
                    or (
                        is_main_wave_shape_pullback
                        and main_wave_stats.get("pullback_shape_ready")
                        and _safe_float(main_wave_stats.get("pct_20")) >= 20.0
                    )
                )
                and _safe_float(main_wave_stats.get("near_high_ratio"), 1.0) >= 0.88
            )
            heavy_outflow = bool(
                not fund_is_stale
                and (current_inflow <= -1e8 or inflow_pct <= -4.0)
            )
            core_confirmation_count = sum(
                (orderbook_confirmed, fund_confirmed, sector_confirmed)
            )
            green_open_reclaim_ready = bool(
                main_wave_memory_ready
                and settings.ANOMALY_MAIN_WAVE_GREEN_OPEN_LOWER_PCT
                <= open_change_pct
                <= settings.ANOMALY_MAIN_WAVE_GREEN_OPEN_UPPER_PCT
                and low_drop_pct
                <= settings.ANOMALY_MAIN_WAVE_GREEN_MIN_LOW_DROP_PCT
                and -3.5 <= change_pct <= 1.8
                and price >= open_price
                and intraday_rebound_pct
                >= settings.ANOMALY_MAIN_WAVE_GREEN_MIN_REBOUND_PCT
                and close_position
                >= settings.ANOMALY_MAIN_WAVE_GREEN_MIN_CLOSE_POSITION
                and pullback_from_high_pct
                >= -settings.ANOMALY_MAIN_WAVE_GREEN_MAX_PULLBACK_FROM_HIGH_PCT
                and price_vs_avg_pct >= -0.8
                and short_momentum_pct
                >= settings.ANOMALY_MAIN_WAVE_GREEN_MIN_MOMENTUM_PCT
                and settings.ANOMALY_MAIN_WAVE_GREEN_MIN_VOLUME_RATIO
                <= volume_ratio
                <= settings.ANOMALY_MAIN_WAVE_GREEN_MAX_VOLUME_RATIO
                and turnover <= settings.ANOMALY_MAIN_WAVE_GREEN_MAX_TURNOVER
                and amplitude <= settings.ANOMALY_MAIN_WAVE_GREEN_MAX_AMPLITUDE
                and incremental_amount_confirmed
                and not acceleration_distribution_risk
                and not heavy_outflow
                and core_confirmation_count >= 2
                and (orderbook_confirmed or fund_confirmed)
            )
            if green_open_reclaim_ready:
                confirmations = [
                    f"主升浪绿开{open_change_pct:.1f}%，最低下探{low_drop_pct:.1f}%",
                    f"从日内低点回抽{intraday_rebound_pct:.1f}%",
                    f"短周期动能{short_momentum_pct:+.1f}%并有增量成交",
                ]
                if rolling_60s_confirmed:
                    confirmations.append(
                        f"滚动60秒上涨{_safe_float(rolling_60s.get('rolling_60s_change_pct')):+.2f}%"
                    )
                if orderbook_confirmed:
                    confirmations.append(f"盘口承接{support_strength:.0f}且买盘占优")
                if fund_confirmed:
                    confirmations.append(f"实时主力净流入占比{inflow_pct:.1f}%")
                if sector_confirmed:
                    confirmations.append("主驱动板块同步转强")

                score = 74.0
                score += min(max(intraday_rebound_pct - 1.2, 0.0), 4.0) * 2.0
                score += min(max(short_momentum_pct - 0.55, 0.0), 1.5) * 3.0
                score += 4.0 if orderbook_confirmed else 0.0
                score += 3.0 if fund_confirmed else 0.0
                score += 3.0 if sector_confirmed else 0.0
                detail = {
                    **common_detail,
                    "signal_type": "main_wave_green_open_reclaim",
                    "signal_label": "主升浪绿开下杀回收",
                    "low_absorb_type": "main_wave_green_reversal",
                    "main_wave_green_open_reclaim_confirmed": True,
                    "main_wave_green_open_reclaim_rolling60": rolling_60s_confirmed,
                    "detection_pool_member": True,
                    "detection_pool_source": "dynamic_main_wave_green_open_reclaim",
                    "low_absorb_confirmations": confirmations[:7],
                    "main_wave_core_confirmation_count": core_confirmation_count,
                    "open_change_pct": round(open_change_pct, 2),
                    "low_drop_pct": round(low_drop_pct, 2),
                    "intraday_rebound_pct": round(intraday_rebound_pct, 2),
                    "close_position": round(close_position, 3),
                    "pullback_from_high_pct": round(pullback_from_high_pct, 2),
                    "scan_change_pct": round(scan_change_pct, 3),
                    "short_momentum_pct": round(short_momentum_pct, 3),
                    **amount_flow,
                    **rolling_60s,
                }
                return (
                    "low_absorb",
                    min(score, 94.0),
                    "主升浪绿开下杀回收",
                    detail,
                )

            # 首阴/缩量十字星低点在盘前已固定；实时只判断“触及后回拉”，
            # 不声称知道当天最终最低点，避免前视偏差。
            rolling_60s_change_pct = _safe_float(
                rolling_60s.get("rolling_60s_change_pct")
            )
            shape_memory_ready = bool(
                is_main_wave_shape_pullback
                and main_wave_stats.get("pullback_shape_ready")
            )
            strict_shape_memory_ready = bool(
                shape_memory_ready
                and str(main_wave_stats.get("pullback_quality_tier") or "")
                == "strict"
            )
            reclaim_shape_memory_ready = bool(
                shape_memory_ready
                and str(main_wave_stats.get("pullback_quality_tier") or "")
                == "reclaim"
                and main_wave_stats.get("pullback_requires_next_session_reclaim")
            )
            technical_persistence_score = _safe_float(
                main_wave_stats.get("pullback_technical_persistence_score")
            )
            technical_persistence_ready = bool(
                # 兼容升级前已经写入内存池的记录；新生成记录必须达到中等
                # 持续性，低分长阴不允许仅靠一次60秒脉冲触发买点。
                "pullback_technical_persistence_score" not in main_wave_stats
                or technical_persistence_score >= 66.0
            )
            direct_event_grade = str(
                main_wave_stats.get("news_event_grade") or ""
            ).strip().lower()
            direct_event_score = _safe_float(
                main_wave_stats.get("news_catalyst_score")
            )
            direct_event_title = str(
                main_wave_stats.get("news_title") or ""
            ).strip()
            direct_hard_catalyst_confirmed = bool(
                direct_event_grade == "hard"
                and direct_event_title
                and (
                    bool(main_wave_stats.get("news_fresh_after_trade_close"))
                    or direct_event_score >= 68.0
                )
            )
            pullback_anchor = _safe_float(
                main_wave_stats.get("pullback_anchor_low")
                if shape_memory_ready
                else support
            )
            pullback_reclaim_price = _safe_float(
                main_wave_stats.get("pullback_reclaim_price"),
                _safe_float(main_wave_stats.get("pullback_anchor_close")),
            )
            support_break_pct = (
                (low_price / pullback_anchor - 1.0) * 100.0
                if pullback_anchor > 0
                else -100.0
            )
            shape_support_touched = bool(
                pullback_anchor > 0
                and low_price <= pullback_anchor * 1.012
                and support_break_pct
                >= -(
                    4.0
                    if reclaim_shape_memory_ready
                    else settings.ANOMALY_MAIN_WAVE_PULLBACK_MAX_SUPPORT_BREAK_PCT
                )
            )
            shape_price_reclaimed = bool(
                price
                >= (
                    pullback_reclaim_price * 0.995
                    if reclaim_shape_memory_ready and pullback_reclaim_price > 0
                    else pullback_anchor * 0.995
                )
            )
            shape_confirmation_ready = bool(
                (
                    fund_confirmed
                    and large_order_confirmed
                    and core_sector_confirmed
                    and sector_leader_confirmed
                )
                if strict_shape_memory_ready
                else (
                    reclaim_shape_memory_ready
                    and (
                        (
                            core_sector_confirmed
                            and core_confirmation_count >= 2
                            and (orderbook_confirmed or fund_confirmed or large_order_confirmed)
                        )
                        # 风范股份一类是“官方硬事件驱动的独立行情”，板块当天
                        # 可以整体走弱。允许硬公告替代板块闸门，但仍必须满足
                        # 首阴收回、滚动60秒增量成交和盘口承接，不能变成消息追高。
                        or (
                            direct_hard_catalyst_confirmed
                            and orderbook_confirmed
                        )
                    )
                )
                if shape_memory_ready
                else (
                    core_confirmation_count >= 2
                    and (orderbook_confirmed or fund_confirmed)
                )
            )
            shape_pullback_ready = bool(
                main_wave_memory_ready
                and (
                    not shape_memory_ready
                    or strict_shape_memory_ready
                    or reclaim_shape_memory_ready
                )
                and technical_persistence_ready
                and shape_support_touched
                and -4.5 <= change_pct <= (3.0 if reclaim_shape_memory_ready else 1.8)
                and shape_price_reclaimed
                and intraday_rebound_pct
                >= settings.ANOMALY_MAIN_WAVE_PULLBACK_MIN_REBOUND_PCT
                and close_position
                >= settings.ANOMALY_MAIN_WAVE_PULLBACK_MIN_CLOSE_POSITION
                and short_momentum_pct
                >= settings.ANOMALY_MAIN_WAVE_PULLBACK_MIN_MOMENTUM_PCT
                and rolling_60s_confirmed
                and rolling_60s_change_pct
                >= settings.ANOMALY_MAIN_WAVE_PULLBACK_MIN_MOMENTUM_PCT
                and settings.ANOMALY_MAIN_WAVE_PULLBACK_MIN_VOLUME_RATIO
                <= volume_ratio
                <= settings.ANOMALY_MAIN_WAVE_PULLBACK_MAX_VOLUME_RATIO
                and amplitude <= settings.ANOMALY_MAIN_WAVE_PULLBACK_MAX_AMPLITUDE
                and turnover <= settings.ANOMALY_MAIN_WAVE_PULLBACK_MAX_TURNOVER
                and price_vs_avg_pct >= -0.8
                and not acceleration_distribution_risk
                and not heavy_outflow
                and shape_confirmation_ready
            )
            if shape_pullback_ready:
                shape_label = str(
                    main_wave_stats.get("pullback_shape_label")
                    if shape_memory_ready
                    else "主升浪趋势支撑"
                )
                anchor_label = (
                    f"{shape_label}低点"
                    if shape_memory_ready
                    else shape_label
                )
                signal_label = (
                    "主升首阴深洗收回"
                    if reclaim_shape_memory_ready
                    else f"{shape_label}低点回收"
                    if shape_memory_ready
                    else "主升浪支撑回收"
                )
                confirmations = [
                    f"{anchor_label}{pullback_anchor:.2f}已触及",
                    f"日内低点回拉{intraday_rebound_pct:.1f}%",
                    f"滚动60秒上涨{rolling_60s_change_pct:+.2f}%且有增量成交",
                ]
                if reclaim_shape_memory_ready and pullback_reclaim_price > 0:
                    confirmations.insert(
                        1,
                        f"已收回首阴收盘价{pullback_reclaim_price:.2f}",
                    )
                if orderbook_confirmed:
                    confirmations.append(f"盘口承接{support_strength:.0f}且买盘占优")
                if fund_confirmed:
                    confirmations.append(f"实时主力净流入占比{inflow_pct:.1f}%")
                if large_order_confirmed:
                    confirmations.append(
                        f"大单净流入{_safe_float(large_order_context.get('large_order_net_inflow')) / 1e6:.0f}百万元"
                    )
                if core_sector_confirmed:
                    core_sector_name = str(
                        (core_sector_driver or {}).get("sector_name") or "主驱动板块"
                    )
                    confirmations.append(
                        f"{core_sector_name}正向周期且产业扩散"
                        + ("，该股为生命周期龙头" if sector_leader_confirmed else "")
                    )
                if direct_hard_catalyst_confirmed:
                    confirmations.append(f"官方硬事件:{direct_event_title[:36]}")
                score = 76.0
                score += min(max(intraday_rebound_pct - 0.65, 0.0), 2.5) * 2.0
                score += min(max(rolling_60s_change_pct - 0.30, 0.0), 1.2) * 4.0
                score += 4.0 if orderbook_confirmed else 0.0
                score += 3.0 if fund_confirmed else 0.0
                score += 3.0 if sector_confirmed else 0.0
                score += 4.0 if direct_hard_catalyst_confirmed else 0.0
                detail = {
                    **common_detail,
                    "signal_type": (
                        "main_wave_shape_pullback_reclaim"
                        if shape_memory_ready
                        else "main_wave_support_reclaim"
                    ),
                    "signal_label": signal_label,
                    "low_absorb_type": (
                        "main_wave_shape_pullback_reclaim"
                        if shape_memory_ready
                        else "main_wave_support_reclaim"
                    ),
                    "main_wave_shape_pullback_confirmed": shape_memory_ready,
                    "main_wave_deep_wash_reclaim_confirmed": reclaim_shape_memory_ready,
                    "main_wave_support_reclaim_confirmed": True,
                    "main_wave_shape_pullback_rolling60": bool(
                        shape_memory_ready and rolling_60s_confirmed
                    ),
                    "main_wave_support_reclaim_rolling60": bool(
                        (not shape_memory_ready) and rolling_60s_confirmed
                    ),
                    "detection_pool_member": True,
                    "detection_pool_source": (
                        "dynamic_main_wave_shape_pullback"
                        if shape_memory_ready
                        else "dynamic_main_wave_support_reclaim"
                    ),
                    "low_absorb_confirmations": confirmations[:7],
                    "main_wave_core_confirmation_count": core_confirmation_count,
                    "main_wave_strict_pullback_confirmed": strict_shape_memory_ready,
                    "main_wave_core_sector_confirmed": core_sector_confirmed,
                    "main_wave_sector_leader_confirmed": sector_leader_confirmed,
                    "main_wave_core_sector_driver": core_sector_driver or {},
                    "main_wave_direct_hard_catalyst_confirmed": direct_hard_catalyst_confirmed,
                    "main_wave_direct_event_title": direct_event_title,
                    "pullback_technical_persistence_score": round(
                        technical_persistence_score, 1
                    ),
                    "pullback_technical_persistence_ready": (
                        technical_persistence_ready
                    ),
                    **large_order_context,
                    "pullback_shape_type": main_wave_stats.get("pullback_shape_type"),
                    "pullback_shape_label": shape_label,
                    "pullback_anchor_low": round(pullback_anchor, 3),
                    "pullback_reclaim_price": round(pullback_reclaim_price, 3),
                    "support_break_pct": round(support_break_pct, 2),
                    "intraday_rebound_pct": round(intraday_rebound_pct, 2),
                    "close_position": round(close_position, 3),
                    "short_momentum_pct": round(short_momentum_pct, 3),
                    **amount_flow,
                    **rolling_60s,
                }
                return (
                    "low_absorb",
                    min(score, 94.0),
                    signal_label,
                    detail,
                )
            if (
                main_wave_memory_ready
                and low_price <= pullback_anchor * 1.02
                and -4.5 <= change_pct <= 1.8
            ):
                # 该观察池只能走专用的60秒回拉路径，不能降级命中下面较宽松的
                # 通用趋势回收规则，否则缩量下跌也可能被误报为买点。
                return None

        if is_leader_linkage_setup:
            link_stats = dict(setup.get("stats") or {})
            leader_code = str(link_stats.get("leader_code") or "")
            leader_name = str(link_stats.get("leader_name") or leader_code)
            if leader_spot is None or str(getattr(leader_spot, "code", "") or "") != leader_code:
                return None
            leader_price = _safe_float(getattr(leader_spot, "price", 0))
            leader_limit_up = _safe_float(getattr(leader_spot, "limit_up", 0))
            leader_high = _safe_float(getattr(leader_spot, "high", 0))
            leader_avg = _safe_float(getattr(leader_spot, "avg_price", 0))
            leader_change = _safe_float(getattr(leader_spot, "change_pct", 0))
            leader_near_high = bool(leader_high > 0 and leader_price >= leader_high * 0.992)
            leader_above_vwap = bool(leader_avg > 0 and leader_price >= leader_avg * 0.998)
            leader_at_limit = bool(leader_limit_up > 0 and leader_price >= leader_limit_up * 0.995)
            leader_stable = bool(
                leader_price > 0
                and (
                    leader_at_limit
                    or (leader_change >= 4.0 and leader_near_high and leader_above_vwap)
                )
            )
            follower_momentum = bool(min5_change >= 0.35 or intraday_rebound_pct >= 1.2)
            linkage_confirmations = [
                label
                for ready, label in (
                    (leader_stable, f"龙头{leader_name}维持前排强势"),
                    (leader_at_limit, f"龙头{leader_name}封板稳定"),
                    (leader_near_high, f"龙头{leader_name}贴近日内高点"),
                    (sector_confirmed, "关联板块涨幅和资金保持正向"),
                    (vwap_reclaimed, "B站回VWAP"),
                    (near_intraday_high, "B贴近日内高点"),
                    (1.15 <= volume_ratio <= 3.0, f"B量比{volume_ratio:.2f}温和放大"),
                    (follower_momentum, f"B短周期动能{max(min5_change, intraday_rebound_pct):+.1f}%"),
                    (orderbook_confirmed, f"B盘口承接{support_strength:.0f}"),
                    (fund_confirmed, f"B主力净流入占比{inflow_pct:.1f}%"),
                )
                if ready
            ]
            linkage_ready = bool(
                _safe_float(link_stats.get("leader_recognition_score")) >= 68
                and _safe_float(link_stats.get("linkage_score")) >= 72
                and _safe_float(link_stats.get("business_relevance_score")) >= 76
                and _safe_float(link_stats.get("follower_shape_score")) >= 68
                and _safe_float(link_stats.get("theme_alignment_score")) >= 75
                and leader_stable
                and sector_confirmed
                and -1.5 <= change_pct <= 5.5
                and vwap_reclaimed
                and near_intraday_high
                and 1.15 <= volume_ratio <= 3.0
                and turnover <= 12.0
                and amplitude <= 8.0
                and follower_momentum
                and (orderbook_confirmed or fund_confirmed)
                and len(linkage_confirmations) >= 6
            )
            if linkage_ready:
                score = 76.0
                score += min(_safe_float(link_stats.get("linkage_score")) - 72.0, 12.0) * 0.45
                score += 4.0 if leader_at_limit else 0.0
                score += 4.0 if orderbook_confirmed else 0.0
                score += 4.0 if fund_confirmed else 0.0
                detail = {
                    **common_detail,
                    "signal_type": "leader_linkage_confirmation",
                    "signal_label": "龙头映射补涨二次确认",
                    "leader_linkage_confirmed": True,
                    "leader_code": leader_code,
                    "leader_name": leader_name,
                    "leader_change_pct": round(leader_change, 2),
                    "leader_at_limit": leader_at_limit,
                    "leader_near_high": leader_near_high,
                    "leader_recognition_score": _safe_float(link_stats.get("leader_recognition_score")),
                    "linkage_score": _safe_float(link_stats.get("linkage_score")),
                    "business_relevance_score": _safe_float(link_stats.get("business_relevance_score")),
                    "follower_shape_score": _safe_float(link_stats.get("follower_shape_score")),
                    "theme_alignment_score": _safe_float(link_stats.get("theme_alignment_score")),
                    "leader_driver_reason": str(link_stats.get("leader_driver_reason") or ""),
                    "leader_industry": str(link_stats.get("leader_industry") or ""),
                    "follower_industry": str(link_stats.get("follower_industry") or ""),
                    "link_sector_name": str(link_stats.get("link_sector_name") or ""),
                    "leader_linkage_confirmations": linkage_confirmations[:8],
                    "quality_score": round(min(score, 94.0), 2),
                    "position_before_trigger": 0,
                }
                return (
                    "breakthrough",
                    min(score, 94.0),
                    "龙头映射补涨二次确认",
                    detail,
                )

        if is_event_relay_setup:
            event_stats = dict(setup.get("stats") or {})
            board_count = max(1, int(event_stats.get("board_count") or 1))
            is_second_board_relay = setup_source == "second_board_relay"
            event_grade = str(
                event_stats.get("news_event_grade")
                or ("relay" if is_second_board_relay else "")
            )
            fresh_after_close = bool(event_stats.get("news_fresh_after_trade_close"))
            open_change_pct = (open_price / prev_close - 1.0) * 100.0 if prev_close > 0 else 0.0
            auction_band_ready = (
                0.5 <= open_change_pct <= 3.5
                if setup_source in {"event_first_board", "second_board_relay"}
                else 1.0 <= open_change_pct <= 4.0
            )
            event_identity_ready = bool(
                (event_grade in {"hard", "medium"} or is_second_board_relay)
                and not bool(event_stats.get("is_one_word"))
                and int(event_stats.get("break_count") or 0) <= 3
                and 2.0 <= _safe_float(event_stats.get("turnover")) <= 22.0
                and (
                    setup_source in {"event_first_board", "second_board_relay"}
                    and (
                        not is_second_board_relay
                        or bool(event_stats.get("relay_pool_ready"))
                    )
                    or (
                        board_count <= 3
                        and event_grade == "hard"
                        and (
                            fresh_after_close
                            or _safe_float(event_stats.get("news_catalyst_score")) >= 68
                        )
                    )
                )
            )
            relay_confirmations = [
                label
                for ready, label in (
                    (auction_band_ready, f"竞价涨幅{open_change_pct:+.1f}%处于接力区间"),
                    (price >= prev_close * 0.995, "守住前日涨停价"),
                    (vwap_reclaimed, "站稳VWAP"),
                    (near_intraday_high, "价格贴近日内高点"),
                    (min5_change >= 0.25, f"5分钟动能{min5_change:+.1f}%"),
                    (1.15 <= volume_ratio <= 3.2, f"量比{volume_ratio:.2f}完成换手"),
                    (orderbook_confirmed, f"盘口承接{support_strength:.0f}"),
                    (fund_confirmed, f"主力净流入占比{inflow_pct:.1f}%"),
                    (sector_confirmed, "主驱动板块前排仍强"),
                    (
                        rolling_60s_confirmed or intraday_amount_confirmed,
                        (
                            f"滚动60秒{_safe_float(rolling_60s.get('rolling_60s_change_pct')):+.2f}%+增量成交"
                            if rolling_60s_confirmed
                            else "盘中增量成交确认"
                        ),
                    ),
                )
                if ready
            ]
            relay_ready = bool(
                event_identity_ready
                and auction_band_ready
                and price >= prev_close * 0.995
                and vwap_reclaimed
                and near_intraday_high
                and 1.15 <= volume_ratio <= 3.2
                and turnover <= 12.0
                and amplitude <= 8.0
                and (min5_change >= 0.25 or intraday_rebound_pct >= 1.0)
                and (rolling_60s_confirmed or intraday_amount_confirmed)
                and sector_confirmed
                and (orderbook_confirmed or fund_confirmed)
                and len(relay_confirmations) >= 6
            )
            if relay_ready:
                score = 78.0
                score += 5.0 if event_grade == "hard" else 4.0 if is_second_board_relay else 2.0
                score += 4.0 if orderbook_confirmed else 0.0
                score += 4.0 if fund_confirmed else 0.0
                score += min(max(min5_change, 0.0), 2.0) * 2.0
                detail = {
                    **common_detail,
                    "signal_type": (
                        "second_board_relay_confirmation"
                        if is_second_board_relay
                        else "event_relay_confirmation"
                    ),
                    "signal_label": (
                        "首板冲二板二次确认"
                        if is_second_board_relay
                        else "重大事件接力二次确认"
                    ),
                    "event_relay_confirmed": True,
                    "event_relay_source": setup_source,
                    "event_grade": event_grade,
                    "event_title": str(event_stats.get("news_title") or ""),
                    "event_score": _safe_float(event_stats.get("news_catalyst_score")),
                    "relay_quality_score": _safe_float(event_stats.get("relay_quality_score")),
                    "quality_score": round(min(score, 94.0), 2),
                    "event_relay_confirmations": relay_confirmations[:8],
                    "open_change_pct": round(open_change_pct, 2),
                    "position_before_trigger": 0,
                    **amount_flow,
                    **rolling_60s,
                }
                return (
                    "breakthrough",
                    min(score, 94.0),
                    "首板冲二板二次确认"
                    if is_second_board_relay
                    else "重大事件接力二次确认",
                    detail,
                )

        if is_second_wave_reset_setup:
            reset_stats = dict(setup.get("stats") or {})
            scan_change_pct = _safe_float((reversal_state or {}).get("scan_change_pct"))
            acceleration_pct = max(min5_change, scan_change_pct)
            reset_drawdown_pct = _safe_float(reset_stats.get("drawdown_pct"))
            reset_type = str(reset_stats.get("reset_type") or "")
            reset_memory_confirmed = bool(
                reset_type in {"high_board_reset", "trend_cascade_reset"}
                and 3 <= int(reset_stats.get("days_since_peak") or 0) <= 15
                and -55.0 <= reset_drawdown_pct <= -12.0
            )
            price_vs_vwap_ready = bool(
                vwap_reclaimed or price_vs_avg_pct >= -0.8
            )
            momentum_confirmed = bool(
                acceleration_pct >= 0.65
                or (intraday_rebound_pct >= 2.5 and near_intraday_high)
            )
            core_confirmation_count = sum(
                (orderbook_confirmed, fund_confirmed, sector_confirmed)
            )
            restart_ready = bool(
                reset_memory_confirmed
                and -2.8 <= change_pct <= 6.5
                and price >= open_price
                and 1.05 <= volume_ratio <= 3.2
                and turnover <= 20.0
                and amplitude <= 12.0
                and near_intraday_high
                and price_vs_vwap_ready
                and momentum_confirmed
                and intraday_amount_confirmed
                and not acceleration_distribution_risk
                and core_confirmation_count >= 2
                and (orderbook_confirmed or fund_confirmed)
            )
            if restart_ready:
                confirmations = [
                    (
                        f"首波{int(reset_stats.get('max_board_streak') or 0)}连板后"
                        if reset_type == "high_board_reset"
                        else f"首波主升{_safe_float(reset_stats.get('first_wave_gain_pct')):.1f}%后"
                    )
                    + f"下杀{abs(reset_drawdown_pct):.1f}%",
                    f"从日内低点急拉{intraday_rebound_pct:.1f}%并贴近日内高点",
                    (
                        f"成交速率"
                        f"{_safe_float(amount_flow.get('intraday_amount_pace_ratio')):.1f}倍 / "
                        f"短周期动能{acceleration_pct:+.1f}%"
                    ),
                ]
                if price_vs_avg_pct >= 0:
                    confirmations.append(f"站回VWAP上方{price_vs_avg_pct:+.1f}%")
                else:
                    confirmations.append(f"距离VWAP仅{price_vs_avg_pct:+.1f}%")
                if orderbook_confirmed:
                    confirmations.append(f"盘口承接{support_strength:.0f}且买盘占优")
                if fund_confirmed:
                    confirmations.append(f"主力净流入占比{inflow_pct:.1f}%")
                if sector_confirmed:
                    confirmations.append("主驱动板块同步转强")

                score = 72.0
                score += min(max(intraday_rebound_pct - 2.5, 0.0), 4.0) * 2.0
                score += min(max(acceleration_pct, 0.0), 2.0) * 3.0
                score += 4.0 if orderbook_confirmed else 0.0
                score += 4.0 if fund_confirmed else 0.0
                score += 4.0 if sector_confirmed else 0.0
                detail = {
                    **common_detail,
                    "signal_type": "second_wave_restart",
                    "signal_label": "高标下杀二波启动预警",
                    "low_absorb_type": "second_wave_restart",
                    "second_wave_restart_confirmed": True,
                    "detection_pool_member": True,
                    "detection_pool_source": "second_wave_reset_watch",
                    "second_wave_reset_type": reset_type,
                    "second_wave_reset_stats": reset_stats,
                    "second_wave_restart_confirmations": confirmations[:7],
                    "low_absorb_confirmations": confirmations[:7],
                    "second_wave_core_confirmation_count": core_confirmation_count,
                    "scan_change_pct": round(scan_change_pct, 3),
                    "acceleration_pct": round(acceleration_pct, 3),
                    "intraday_rebound_pct": round(intraday_rebound_pct, 2),
                    "near_intraday_high": near_intraday_high,
                    "price_vs_avg_pct": round(price_vs_avg_pct, 2),
                }
                return (
                    "low_absorb",
                    min(score, 94.0),
                    "高标下杀二波启动预警",
                    detail,
                )

        pullback_confirmations = [
            label
            for ready, label in (
                (-1.5 <= support_gap_pct <= 3.0 and low_price <= support * 1.02, "回踩趋势支撑后收回"),
                (vwap_reclaimed, "重新站回VWAP"),
                (min5_change >= 0.35, f"5分钟动能{min5_change:+.1f}%"),
                (intraday_rebound_pct >= 0.8, f"日内低点回抽{intraday_rebound_pct:.1f}%"),
                (orderbook_confirmed, f"盘口承接{support_strength:.0f}"),
                (fund_confirmed, f"主力净流入占比{inflow_pct:.1f}%"),
                (sector_confirmed, "主驱动板块同步转强"),
            )
            if ready
        ]
        # 潜力股不因评分直接获得买点：相较通用趋势回收，强制同窗60秒
        # 价升量增、VWAP修复且涨幅仍在低吸窗口，避免把动能追涨包装成低吸。
        potential_rolling_change_pct = _safe_float(
            rolling_60s.get("rolling_60s_change_pct")
        )
        potential_volume_ready = bool(
            rolling_60s_confirmed
            and rolling_60s.get("rolling_60s_path_confirmed", True)
            and potential_rolling_change_pct
            >= settings.ANOMALY_MAIN_WAVE_PULLBACK_MIN_MOMENTUM_PCT
        )
        pre_board_intraday_ready = bool(
            not is_pre_board_setup
            or (
                long_cycle_shape_ready
                and rolling_60s_confirmed
                and bool(rolling_60s.get("rolling_60s_path_confirmed", True))
                and _safe_float(rolling_60s.get("rolling_60s_amount_pace_ratio"))
                >= settings.ANOMALY_ACCELERATION_MIN_AMOUNT_PACE_RATIO
                and vwap_reclaimed
                and change_pct <= 3.8
                and not acceleration_distribution_risk
                and sum((orderbook_confirmed, fund_confirmed, sector_confirmed)) >= 2
                and (
                    core_sector_confirmed
                    or large_order_confirmed
                    or (orderbook_confirmed and fund_confirmed)
                )
            )
        )
        pullback_ready = (
            str(setup.get("setup_state") or "").startswith("armed_")
            and -3.0 <= change_pct <= (1.8 if is_tenbagger_pullback_setup else 2.5)
            and -1.5 <= support_gap_pct <= 3.0
            and low_price <= support * 1.02
            and price >= support * 0.995
            and intraday_rebound_pct >= 0.8
            and 0.75 <= volume_ratio <= 2.2
            and turnover <= 15.0
            and amplitude <= 7.0
            and len(pullback_confirmations) >= 3
            and (orderbook_confirmed or fund_confirmed)
            and (vwap_reclaimed or min5_change >= 0.5 or sector_confirmed)
            and pre_board_intraday_ready
            and (
                not is_tenbagger_pullback_setup
                or (
                    potential_volume_ready
                    and vwap_reclaimed
                    and not acceleration_distribution_risk
                    and sum((orderbook_confirmed, fund_confirmed, sector_confirmed)) >= 2
                )
            )
        )
        if pullback_ready:
            score = 78.0
            score += 5.0 if orderbook_confirmed else 0.0
            score += 5.0 if fund_confirmed else 0.0
            score += 4.0 if sector_confirmed else 0.0
            score += min(max(intraday_rebound_pct - 0.8, 0.0), 2.0) * 2.0
            detail = {
                **common_detail,
                "signal_type": "trend_driver_low_absorb",
                "signal_label": (
                    "低位预期支撑回收"
                    if is_low_expectation_setup
                    else "牛股潜质支撑回收"
                    if is_tenbagger_pullback_setup
                    else
                    "试盘缩量洗盘支撑回收"
                    if is_probe_wash_setup
                    else "趋势驱动支撑回收"
                ),
                "low_absorb_type": "trend_driver_support_reclaim",
                "low_absorb_confirmations": pullback_confirmations[:6],
                "intraday_rebound_pct": round(intraday_rebound_pct, 2),
                "tenbagger_pullback_confirmed": is_tenbagger_pullback_setup,
                "tenbagger_score": _safe_float((setup.get("stats") or {}).get("tenbagger_score")),
                "potential_quality_score": _safe_float((setup.get("stats") or {}).get("potential_quality_score")),
                **(rolling_60s if is_tenbagger_pullback_setup or is_pre_board_setup else {}),
            }
            description = (
                "低位预期支撑回收"
                if is_low_expectation_setup
                else "牛股潜质支撑回收"
                if is_tenbagger_pullback_setup
                else "试盘缩量洗盘支撑回收"
                if is_probe_wash_setup
                else "趋势驱动支撑回收"
            )
            return "low_absorb", min(score, 94.0), description, detail

        breakout_confirmations = [
            label
            for ready, label in (
                (near_intraday_high, "突破后贴近日内高点"),
                (orderbook_confirmed, f"盘口承接{support_strength:.0f}"),
                (fund_confirmed, f"主力净流入占比{inflow_pct:.1f}%"),
                (sector_confirmed, "主驱动板块同步转强"),
                (min5_change >= 0.5, f"5分钟动能{min5_change:+.1f}%"),
                (vwap_reclaimed, "站稳VWAP"),
            )
            if ready
        ]
        breakout_ready = (
            (not is_tenbagger_pullback_setup or is_low_expectation_setup)
            and
            0.1 <= breakout_pct <= 4.0
            and 1.0 <= change_pct <= 6.5
            and 1.25 <= volume_ratio <= 3.0
            and 1.0 <= turnover <= 15.0
            and amplitude <= 8.0
            and near_intraday_high
            and len(breakout_confirmations) >= 3
            and (orderbook_confirmed or fund_confirmed or sector_confirmed)
            and (
                not is_low_expectation_setup
                or (
                    rolling_60s_confirmed
                    and bool(rolling_60s.get("rolling_60s_path_confirmed", True))
                    and _safe_float(rolling_60s.get("rolling_60s_amount_pace_ratio"))
                    >= settings.ANOMALY_ACCELERATION_MIN_AMOUNT_PACE_RATIO
                    and vwap_reclaimed
                    and not acceleration_distribution_risk
                    and sum((orderbook_confirmed, fund_confirmed, sector_confirmed)) >= 2
                )
            )
            and (
                not is_pre_board_setup
                or (
                    long_cycle_shape_ready
                    and rolling_60s_confirmed
                    and bool(rolling_60s.get("rolling_60s_path_confirmed", True))
                    and _safe_float(rolling_60s.get("rolling_60s_amount_pace_ratio"))
                    >= settings.ANOMALY_ACCELERATION_MIN_AMOUNT_PACE_RATIO
                    and vwap_reclaimed
                    and not acceleration_distribution_risk
                    and sum((orderbook_confirmed, fund_confirmed, sector_confirmed)) >= 2
                    and (
                        core_sector_confirmed
                        or large_order_confirmed
                        or (orderbook_confirmed and fund_confirmed)
                    )
                )
            )
        )
        if breakout_ready:
            score = 80.0
            score += min(max(volume_ratio - 1.25, 0.0), 1.5) * 4.0
            score += 4.0 if orderbook_confirmed else 0.0
            score += 4.0 if fund_confirmed else 0.0
            score += 4.0 if sector_confirmed else 0.0
            detail = {
                **common_detail,
                "signal_type": "trend_driver_breakthrough",
                "signal_label": (
                    "低位预期放量启动"
                    if is_low_expectation_setup
                    else "试盘缩量洗盘放量突破"
                    if is_probe_wash_setup
                    else "趋势驱动放量突破"
                ),
                "quality_score": round(min(score, 96.0), 1),
                "ma_status": (
                    "multi_long"
                    if _safe_float(setup.get("ma5")) > _safe_float(setup.get("ma10")) > _safe_float(setup.get("ma20")) > 0
                    else "long"
                ),
                "days_near_pressure": 0,
                "pullback_probability": "low" if breakout_pct <= 2.5 and volume_ratio <= 2.5 else "medium",
                "is_false_breakout": False,
                "watchlist_confirmations": breakout_confirmations[:6],
                "breakout_anchor": round(resistance, 2),
                **(rolling_60s if is_pre_board_setup or is_low_expectation_setup else {}),
            }
            description = (
                "低位预期放量启动"
                if is_low_expectation_setup
                else "试盘缩量洗盘放量突破"
                if is_probe_wash_setup
                else "趋势驱动放量突破"
            )
            return "breakthrough", min(score, 96.0), description, detail

        # 支撑到达只做A2前置提醒；确认回抽0.8%后仍由上面的support_reclaim升级。
        from_low_pct = (price / low_price - 1.0) * 100.0
        support_touched = bool(
            low_price <= support * 1.01
            and low_price >= support * 0.985
            and settings.ANOMALY_TREND_SUPPORT_TOUCH_MIN_GAP_PCT
            <= support_gap_pct
            <= settings.ANOMALY_TREND_SUPPORT_TOUCH_MAX_GAP_PCT
        )
        near_intraday_low = (
            from_low_pct <= settings.ANOMALY_TREND_SUPPORT_TOUCH_MAX_FROM_LOW_PCT
        )
        momentum_not_cascading = (
            min5_change >= settings.ANOMALY_TREND_SUPPORT_TOUCH_MIN_MIN5_CHANGE_PCT
        )
        heavy_outflow = bool(
            not fund_is_stale
            and (current_inflow <= -1e8 or inflow_pct <= -4.0)
        )
        touch_core_confirmation_count = sum(
            (orderbook_confirmed, fund_confirmed, sector_confirmed)
        )
        touch_ready = bool(
            str(setup.get("setup_state") or "").startswith("armed_")
            and -3.5 <= change_pct <= 1.5
            and support_touched
            and near_intraday_low
            and momentum_not_cascading
            and 0.65 <= volume_ratio <= 1.9
            and turnover <= 12.5
            and amplitude <= 6.5
            and not heavy_outflow
            and touch_core_confirmation_count >= 2
            and (orderbook_confirmed or fund_confirmed)
        )
        if touch_ready:
            touch_confirmations = [
                f"日内最低{low_price:.2f}已触及趋势支撑{support:.2f}",
                f"现价距日内低点仅{from_low_pct:.1f}%",
                f"5分钟跌速{min5_change:+.1f}%，未形成加速破位",
            ]
            if orderbook_confirmed:
                touch_confirmations.append(f"盘口承接{support_strength:.0f}且买盘占优")
            if fund_confirmed:
                touch_confirmations.append(f"主力净流入占比{inflow_pct:.1f}%")
            if sector_confirmed:
                touch_confirmations.append("主驱动板块仍保持正向")

            score = 68.0
            score += 5.0 if orderbook_confirmed else 0.0
            score += 4.0 if fund_confirmed else 0.0
            score += 4.0 if sector_confirmed else 0.0
            score += 3.0 if abs(support_gap_pct) <= 0.6 else 0.0
            detail = {
                **common_detail,
                "signal_type": "trend_support_touch",
                "signal_label": "趋势支撑到达预警",
                "low_absorb_type": "trend_support_touch",
                "trend_support_touch_confirmed": True,
                "detection_pool_member": True,
                "detection_pool_source": "dynamic_trend_support_touch",
                "support_touch_confirmations": touch_confirmations[:6],
                "low_absorb_confirmations": touch_confirmations[:6],
                "support_touch_core_confirmation_count": touch_core_confirmation_count,
                "from_intraday_low_pct": round(from_low_pct, 2),
                "near_intraday_low": near_intraday_low,
                "intraday_rebound_pct": round(from_low_pct, 2),
                "wait_reclaim_confirmation": True,
            }
            return (
                "low_absorb",
                min(score, 84.0),
                "趋势支撑到达预警",
                detail,
            )

        return None

    def _detect_low_base_watch_setup(
        self,
        *,
        spot: StockSpot,
        tech: dict,
        current_fund: dict,
        sector_context: dict,
        board_context: dict,
        today: date,
        fund_source: str,
    ) -> tuple[str, float, str, dict] | None:
        """核心成长观察池的低位启动确认。

        只产生两类可执行信号：平台放量突破，或多头结构内回踩MA5后弱转强。
        """
        if spot.code not in self.low_base_watchlist_codes:
            return None

        net_profit_growth = _safe_float(getattr(spot, "net_profit_growth", None))
        pe_ttm = _safe_float(getattr(spot, "pe_ttm", None))
        circ_market_cap = _safe_float(getattr(spot, "circ_market_cap", None))
        if not (
            settings.ANOMALY_CORE_GROWTH_MIN_NET_PROFIT_GROWTH
            <= net_profit_growth
            <= settings.ANOMALY_CORE_GROWTH_MAX_NET_PROFIT_GROWTH
        ):
            return None
        if not (0 < pe_ttm <= settings.ANOMALY_CORE_GROWTH_MAX_PE_TTM):
            return None
        if not (0 < circ_market_cap <= settings.ANOMALY_CORE_GROWTH_MAX_CIRC_MARKET_CAP):
            return None

        profile = tech.get("low_base_profile") or {}
        closes = np.asarray(profile.get("closes") or [], dtype=float)
        highs = np.asarray(profile.get("highs") or [], dtype=float)
        lows = np.asarray(profile.get("lows") or [], dtype=float)
        if len(closes) < 60 or len(highs) < 60 or len(lows) < 60:
            return None

        price = _safe_float(spot.price)
        low_price = _safe_float(spot.low)
        high_price = _safe_float(spot.high)
        open_price = _safe_float(spot.open)
        avg_price = _safe_float(spot.avg_price)
        prev_close = _safe_float(spot.prev_close)
        if min(price, low_price, prev_close) <= 0:
            return None

        def dynamic_ma(period: int) -> float:
            history_size = max(period - 1, 0)
            if len(closes) < history_size:
                return 0.0
            values = np.append(closes[-history_size:] if history_size else [], price)
            return float(values.mean()) if len(values) else 0.0

        def period_return(period: int) -> float | None:
            if len(closes) < period or closes[-period] <= 0:
                return None
            return float((price / closes[-period] - 1.0) * 100.0)

        ma5 = dynamic_ma(5)
        ma10 = dynamic_ma(10)
        ma20 = dynamic_ma(20)
        ma60 = dynamic_ma(60)
        if min(ma5, ma10, ma20) <= 0:
            return None

        high_20d = float(highs[-20:].max())
        high_60d = float(highs[-60:].max())
        low_120d = float(lows[-min(120, len(lows)):].min())
        high_120d = float(highs[-min(120, len(highs)):].max())
        position_120d = (
            (price - low_120d) / (high_120d - low_120d) * 100.0
            if high_120d > low_120d
            else 50.0
        )
        range_20d = (high_20d / float(lows[-20:].min()) - 1.0) * 100.0
        ma20_five_days_ago = (
            float(closes[-24:-4].mean()) if len(closes) >= 24 else ma20
        )
        ma20_slope_5d = (
            (ma20 / ma20_five_days_ago - 1.0) * 100.0
            if ma20_five_days_ago > 0
            else 0.0
        )
        return_20d = period_return(20)
        return_60d = period_return(60)
        return_120d = period_return(120)
        breakout_distance_pct = (price / high_20d - 1.0) * 100.0 if high_20d > 0 else -100.0

        if return_20d is None or return_60d is None:
            return None
        if not (-3.0 <= return_20d <= 25.0 and -20.0 <= return_60d <= 35.0):
            return None
        if return_120d is not None and return_120d > 45.0:
            return None
        if not (10.0 <= position_120d <= settings.ANOMALY_CORE_GROWTH_MAX_POSITION_120D):
            return None
        if range_20d > 32.0 or ma20_slope_5d < -0.35:
            return None
        if not (price >= ma20 * 0.985 and ma5 >= ma10 * 0.99 and ma10 >= ma20 * 0.985):
            return None
        if int(board_context.get("max_recent_consecutive_days") or 0) >= 2:
            return None
        if int(board_context.get("recent_limit_up_hits") or 0) > 1:
            return None

        change_pct = _safe_float(spot.change_pct)
        min5_change = _spot_effective_min5_change(spot)
        volume_ratio = _safe_float(spot.volume_ratio, 1.0)
        amplitude = _safe_float(spot.amplitude)
        turnover = _safe_float(spot.turnover)
        support_strength = _safe_float(getattr(spot, "support_strength_score", 0))
        imbalance = _safe_float(getattr(spot, "orderbook_imbalance", 0))
        bid_depth_5 = _safe_float(getattr(spot, "bid_depth_5", 0))
        ask_depth_5 = _safe_float(getattr(spot, "ask_depth_5", 0))
        net_amount = _safe_float(current_fund.get("main_net_inflow"))
        inflow_pct = _safe_float(current_fund.get("main_net_inflow_pct"))
        source = str(current_fund.get("source") or fund_source or "stock_spot")
        is_stale = bool(current_fund.get("is_stale"))

        orderbook_confirmed = (
            support_strength >= 60
            and imbalance >= 0.05
            and bid_depth_5 > ask_depth_5 * 1.05
        )
        fund_confirmed = not is_stale and net_amount > 0 and inflow_pct >= 2.5
        sector_confirmed = self._has_positive_sector_driver(sector_context)
        vwap_reclaimed = avg_price > 0 and price >= avg_price * 0.998
        near_intraday_high = high_price > 0 and price >= high_price * 0.992
        intraday_rebound_pct = (price / low_price - 1.0) * 100.0
        price_vs_avg_pct = (price / avg_price - 1.0) * 100.0 if avg_price > 0 else 0.0
        distance_to_ma5_pct = (price / ma5 - 1.0) * 100.0

        common_detail = {
            "watchlist_member": True,
            "low_base_watchlist": True,
            "low_base_setup_confirmed": True,
            "watchlist_label": "核心成长观察池",
            "core_growth_watchlist": True,
            "core_growth_qualified": True,
            "core_growth_logic": CORE_GROWTH_LOGIC_BY_CODE.get(spot.code, "主营业务成长待持续验证"),
            "core_growth_confirmations": [
                f"净利润增速{net_profit_growth:+.1f}%",
                f"PE(TTM) {pe_ttm:.1f}倍",
                f"流通市值{circ_market_cap:.1f}亿",
            ],
            "low_base_shape_confirmed": True,
            "return_20d": round(return_20d, 2),
            "return_60d": round(return_60d, 2),
            "return_120d": round(return_120d, 2) if return_120d is not None else None,
            "position_120d": round(position_120d, 2),
            "range_20d": round(range_20d, 2),
            "breakout_distance_pct": round(breakout_distance_pct, 2),
            "ma20_slope_5d": round(ma20_slope_5d, 2),
            "high_20d": round(high_20d, 2),
            "high_60d": round(high_60d, 2),
            "ma5": round(ma5, 2),
            "ma10": round(ma10, 2),
            "ma20": round(ma20, 2),
            "ma60": round(ma60, 2),
            "main_net_inflow": net_amount,
            "main_net_inflow_pct": inflow_pct,
            "source": source,
            "as_of": str(current_fund.get("as_of") or today),
            "is_stale": is_stale,
            **self._build_quote_detail(spot),
            **self._build_orderbook_detail(spot),
            **sector_context,
            **board_context,
        }

        breakout_confirmations = [
            label
            for ready, label in (
                (orderbook_confirmed, f"盘口承接{support_strength:.0f}且买盘占优"),
                (fund_confirmed, f"主力净流入占比{inflow_pct:.1f}%"),
                (sector_confirmed, "主驱动板块同步转强"),
                (vwap_reclaimed and near_intraday_high, "站回VWAP且贴近日内高点"),
                (min5_change >= 0.5, f"5分钟动能{min5_change:+.1f}%"),
            )
            if ready
        ]
        breakout_ready = (
            -0.20 <= breakout_distance_pct <= 4.5
            and 1.5 <= change_pct <= 6.2
            and 1.35 <= volume_ratio <= 3.0
            and 1.0 <= turnover <= 12.0
            and amplitude <= 8.0
            and near_intraday_high
            and len(breakout_confirmations) >= 2
            and (orderbook_confirmed or fund_confirmed or sector_confirmed)
        )
        if breakout_ready:
            days_near_pressure = 0
            for close in reversed(closes[-10:]):
                if close >= high_20d * 0.94:
                    days_near_pressure += 1
                else:
                    break
            quality_score = min(
                96.0,
                80.0
                + min(max(volume_ratio - 1.35, 0.0), 1.15) * 6.0
                + min(days_near_pressure, 5) * 1.5
                + (3.0 if orderbook_confirmed else 0.0)
                + (3.0 if fund_confirmed else 0.0),
            )
            score = min(94.0, quality_score + (2.0 if sector_confirmed else 0.0))
            detail = {
                **common_detail,
                "signal_type": "low_base_breakthrough",
                "signal_label": "核心成长平台放量确认",
                "quality_score": round(quality_score, 1),
                "ma_status": "multi_long" if ma5 > ma10 > ma20 > ma60 else "long",
                "pullback_probability": (
                    "low"
                    if breakout_distance_pct <= 2.8 and volume_ratio <= 2.5 and days_near_pressure >= 2
                    else "medium"
                ),
                "days_near_pressure": days_near_pressure,
                "is_false_breakout": False,
                "watchlist_confirmations": breakout_confirmations[:5],
            }
            return "breakthrough", score, "核心成长平台放量确认", detail

        ma5_pullback = (
            -1.5 <= distance_to_ma5_pct <= 2.5
            and low_price <= ma5 * 1.015
            and price >= ma5 * 0.995
        )
        green_reversal = (
            -3.0 <= change_pct <= 1.2
            and intraday_rebound_pct >= 0.8
            and min5_change >= 0.35
            and (
                vwap_reclaimed
                or (open_price > 0 and price >= open_price * 0.998)
            )
        )
        low_absorb_confirmations = [
            label
            for ready, label in (
                (ma5_pullback, "多头结构内回踩MA5不破"),
                (green_reversal, f"盘中下探后回抽{intraday_rebound_pct:.1f}%"),
                (orderbook_confirmed, f"盘口承接{support_strength:.0f}且买盘占优"),
                (fund_confirmed, f"主力净流入占比{inflow_pct:.1f}%"),
                (sector_confirmed, "主驱动板块同步转强"),
                (vwap_reclaimed, "重新站回VWAP附近"),
            )
            if ready
        ]
        low_absorb_ready = (
            (ma5_pullback or green_reversal)
            and -3.0 <= change_pct <= 1.8
            and 0.75 <= volume_ratio <= 2.2
            and amplitude <= 6.5
            and turnover <= 12.0
            and len(low_absorb_confirmations) >= 3
            and (orderbook_confirmed or fund_confirmed)
            and (sector_confirmed or vwap_reclaimed or min5_change >= 0.5)
        )
        if low_absorb_ready:
            score = 74.0
            score += 5.0 if ma5_pullback else 0.0
            score += 5.0 if green_reversal else 0.0
            score += 4.0 if orderbook_confirmed else 0.0
            score += 4.0 if fund_confirmed else 0.0
            score += 3.0 if sector_confirmed else 0.0
            detail = {
                **common_detail,
                "signal_type": "low_base_low_absorb",
                "signal_label": "核心成长回踩低吸",
                "low_absorb_type": (
                    "watchlist_green_reversal_ma5_pullback"
                    if green_reversal and ma5_pullback
                    else "watchlist_green_reversal"
                    if green_reversal
                    else "watchlist_ma5_pullback"
                ),
                "intraday_rebound_pct": round(intraday_rebound_pct, 2),
                "price_vs_avg_pct": round(price_vs_avg_pct, 2),
                "distance_to_ma5_pct": round(distance_to_ma5_pct, 2),
                "low_absorb_confirmations": low_absorb_confirmations[:6],
            }
            return "low_absorb", min(score, 92.0), "核心成长回踩低吸", detail

        return None

    def _build_orderbook_detail(self, spot: StockSpot) -> dict:
        bid_depth_5 = int(getattr(spot, "bid_depth_5", 0) or 0)
        ask_depth_5 = int(getattr(spot, "ask_depth_5", 0) or 0)
        return {
            "bid1_price": float(getattr(spot, "bid1_price", 0) or 0),
            "bid1_volume": int(getattr(spot, "bid1_volume", 0) or 0),
            "ask1_price": float(getattr(spot, "ask1_price", 0) or 0),
            "ask1_volume": int(getattr(spot, "ask1_volume", 0) or 0),
            "bid_depth_5": bid_depth_5,
            "ask_depth_5": ask_depth_5,
            "orderbook_imbalance": _safe_float(
                getattr(spot, "orderbook_imbalance", None), None
            ),
            "bid_ask_spread": _safe_float(
                getattr(spot, "bid_ask_spread", None), None
            ),
            "seal_quality_score": _safe_float(
                getattr(spot, "seal_quality_score", None), None
            ),
            "support_strength_score": _safe_float(
                getattr(spot, "support_strength_score", None), None
            ),
            "withdrawal_ratio": _safe_float(
                getattr(spot, "withdrawal_ratio", None), None
            ),
        }

    def _build_quote_detail(self, spot: StockSpot) -> dict:
        """统一透传实时行情关键字段，供推送模板/前端展示复用"""
        amount_flow = dict(getattr(spot, "_intraday_amount_flow", {}) or {})
        rolling_60s = dict(getattr(spot, "_rolling_60s_momentum", {}) or {})
        source_quote_at = _as_quote_datetime(getattr(spot, "source_quote_at", None))
        received_at = _as_quote_datetime(getattr(spot, "received_at", None))
        updated_at = _as_quote_datetime(getattr(spot, "updated_at", None))
        return {
            "price": float(getattr(spot, "price", 0) or 0),
            "prev_close": float(getattr(spot, "prev_close", 0) or 0),
            "open": float(getattr(spot, "open", 0) or 0),
            "high": float(getattr(spot, "high", 0) or 0),
            "low": float(getattr(spot, "low", 0) or 0),
            "change_pct": float(getattr(spot, "change_pct", 0) or 0),
            "change_amt": float(getattr(spot, "change_amt", 0) or 0),
            "amplitude": float(getattr(spot, "amplitude", 0) or 0),
            "volume": float(getattr(spot, "volume", 0) or 0),
            "amount": float(getattr(spot, "amount", 0) or 0),
            "turnover": float(getattr(spot, "turnover", 0) or 0),
            "volume_ratio": float(getattr(spot, "volume_ratio", 0) or 0),
            "avg_price": float(getattr(spot, "avg_price", 0) or 0),
            "circ_market_cap": float(getattr(spot, "circ_market_cap", 0) or 0),
            "pe_ttm": float(getattr(spot, "pe_ttm", 0) or 0),
            "pb": float(getattr(spot, "pb", 0) or 0),
            "dividend_yield": float(getattr(spot, "dividend_yield", 0) or 0),
            "net_profit_growth": float(getattr(spot, "net_profit_growth", 0) or 0),
            "limit_up": float(getattr(spot, "limit_up", 0) or 0),
            "limit_down": float(getattr(spot, "limit_down", 0) or 0),
            # Independent quote clocks: fund metadata must not relabel the book.
            "quote_source_at": source_quote_at.isoformat() if source_quote_at else None,
            "quote_received_at": received_at.isoformat() if received_at else None,
            "source_quote_at": source_quote_at.isoformat() if source_quote_at else None,
            "received_at": received_at.isoformat() if received_at else None,
            "updated_at": updated_at.isoformat() if updated_at else None,
            "quote_round_id": getattr(spot, "quote_round_id", None),
            "min5_change": (
                _spot_effective_min5_change(spot)
                if _spot_min5_available(spot)
                else None
            ),
            "min5_change_available": _spot_min5_available(spot),
            "intraday_amount_confirmed": bool(
                amount_flow.get("intraday_amount_confirmed")
            ),
            "intraday_amount_delta": _safe_float(
                amount_flow.get("intraday_amount_delta")
            ),
            "intraday_amount_interval_sec": _safe_float(
                amount_flow.get("intraday_amount_interval_sec")
            ),
            "intraday_amount_pace_ratio": _safe_float(
                amount_flow.get("intraday_amount_pace_ratio")
            ),
            "rolling_60s_confirmed": bool(
                rolling_60s.get("rolling_60s_confirmed")
            ),
            "rolling_60s_change_pct": _safe_float(
                rolling_60s.get("rolling_60s_change_pct")
            ),
            "rolling_60s_interval_sec": _safe_float(
                rolling_60s.get("rolling_60s_interval_sec")
            ),
            "rolling_60s_amount_delta": _safe_float(
                rolling_60s.get("rolling_60s_amount_delta")
            ),
            "rolling_60s_amount_pace_ratio": _safe_float(
                rolling_60s.get("rolling_60s_amount_pace_ratio")
            ),
            "rolling_60s_tier": str(
                rolling_60s.get("rolling_60s_tier") or ""
            ),
        }

    def _build_sector_context_detail(
        self,
        code: str,
        code_sector_map: dict[str, list[dict]],
        sector_persistence_map: dict[str, SectorPersistence],
        sector_limitup_components: dict[str, list[dict]],
        sector_lifecycle_map: dict[str, SectorLifecycle] | None = None,
        recent_sector_leader_codes: dict[str, set[str]] | None = None,
    ) -> dict:
        mappings = code_sector_map.get(code) or []
        if not mappings:
            return {"sector_factors": [], "sector_components": []}

        sectors = []
        for item in mappings:
            sector_code = item.get("sector_code") or ""
            sp = sector_persistence_map.get(sector_code)
            peers = [
                peer for peer in sector_limitup_components.get(sector_code, [])
                if peer.get("code") != code
            ]
            change_pct = round(float(getattr(sp, "change_pct", 0) or 0), 2) if sp else 0.0
            fund_flow = round(float(getattr(sp, "fund_flow", 0) or 0), 2) if sp else 0.0
            strength_score = round(float(getattr(sp, "strength_score", 0) or 0), 1) if sp else 0.0
            consecutive_days = int(getattr(sp, "consecutive_days", 0) or 0) if sp else 0
            limit_up_count = int(getattr(sp, "limit_up_count", 0) or 0) if sp else 0
            sector_type = str(item.get("sector_type") or "")
            lifecycle = (sector_lifecycle_map or {}).get(sector_code)
            lifecycle_state = str(getattr(lifecycle, "lifecycle_state", "") or "")
            lifecycle_score = round(
                _safe_float(getattr(lifecycle, "state_score", 0)), 1
            )
            active_days = int(getattr(lifecycle, "active_days", 0) or 0)
            is_sector_leader = code in (
                (recent_sector_leader_codes or {}).get(sector_code) or set()
            )

            relevance_score = 0.0
            if sector_type == "concept":
                relevance_score += 2.0
            elif sector_type == "industry":
                relevance_score += 0.8
            if peers:
                relevance_score += 8.0 if len(peers) == 1 else 11.0
            relevance_score += min(limit_up_count, 6) * 1.4
            relevance_score += min(consecutive_days, 5) * 1.2
            relevance_score += min(max(strength_score, 0.0), 100.0) / 25.0
            relevance_score += min(max(fund_flow, 0.0), 120.0) / 20.0
            relevance_score += min(max(change_pct, 0.0), 6.0) * 1.4
            if fund_flow < 0:
                relevance_score -= min(abs(fund_flow), 120.0) / 30.0
            if change_pct < 0:
                relevance_score -= min(abs(change_pct), 4.0) * 0.8

            sectors.append(
                {
                    "sector_code": sector_code,
                    "sector_name": item.get("sector_name") or sector_code,
                    "sector_type": sector_type,
                    "source": str(item.get("source") or ""),
                    "change_pct": change_pct,
                    "fund_flow": fund_flow,
                    "strength_score": strength_score,
                    "consecutive_days": consecutive_days,
                    "limit_up_count": limit_up_count,
                    "peer_count": len(peers),
                    "lifecycle_state": lifecycle_state,
                    "lifecycle_score": lifecycle_score,
                    "active_days": active_days,
                    "is_sector_leader": is_sector_leader,
                    "relevance_score": round(relevance_score, 2),
                }
            )

        sectors.sort(
            key=lambda item: (
                -float(item.get("relevance_score") or 0),
                -int(item.get("peer_count") or 0),
                -float(item.get("strength_score") or 0),
                str(item.get("sector_name") or ""),
            )
        )

        # 主驱动不能从任意上涨标签里挑最高分。融资融券、股通、预增等
        # 大范围属性会让完全不同主营的股票被错误归入同一热点，只保留有因果
        # 含义的同花顺产业/题材；其余仅作为页面参考，不参与买点共振。
        #
        # 2026-09-17：概念与行业不再共用同一个资金门槛 —— 行业资金流是二级
        # 行业摊薄到三级的聚合量，与概念不可比，故行业只判价格口径。
        # 判据与理由见 `_is_positive_sector_candidate`。
        positive_candidates = [
            item for item in sectors if _is_positive_sector_candidate(item)
        ]
        reference_candidates = [item for item in sectors if item not in positive_candidates]
        causal_positive_candidates = [
            item for item in positive_candidates
            if _is_causal_trade_driver_sector(item)
        ]
        # 有产业主驱动时绝不让泛标签抢第一；旧数据没有有效产业映射时仍保留
        # 最强上涨板块供页面参考，但后续买点确认会拒绝其作为主驱动。
        primary_pool = causal_positive_candidates or positive_candidates

        selected_sectors = []
        if primary_pool:
            selected_sectors.append(primary_pool[0])
            if len(primary_pool) > 1:
                top_score = float(primary_pool[0].get("relevance_score") or 0)
                second = primary_pool[1]
                second_score = float(second.get("relevance_score") or 0)
                if (
                    second_score >= max(top_score * 0.72, top_score - 3.0)
                    and (
                        second.get("peer_count", 0) > 0
                        or second.get("limit_up_count", 0) >= 2
                        or second.get("fund_flow", 0) > 0
                    )
                ):
                    selected_sectors.append(second)

        # 主升回踩需要看到真正的产业主驱动。即使泛概念相关度更高，也要把
        # 正向生命周期的产业板块带入上下文，供严格A2门槛使用。
        core_sector_candidates = [
            sector for sector in sectors
            if _is_main_wave_core_sector_factor(sector)
        ]
        if core_sector_candidates:
            core_sector_candidates.sort(
                key=lambda item: (
                    bool(item.get("is_sector_leader")),
                    _safe_float(item.get("lifecycle_score")),
                    _safe_float(item.get("strength_score")),
                    int(item.get("limit_up_count") or 0),
                ),
                reverse=True,
            )
            strongest_core = core_sector_candidates[0]
            if strongest_core not in selected_sectors:
                selected_sectors.insert(0, strongest_core)
        selected_sectors = selected_sectors[:3]

        sector_components = []
        for sector in selected_sectors:
            peers = []
            for peer in sector_limitup_components.get(sector["sector_code"], []):
                if peer.get("code") == code:
                    continue
                peers.append(peer)
            if not peers:
                continue
            sector_components.append(
                {
                    "sector_name": sector["sector_name"],
                    "leaders": peers[:3],
                }
            )

        return {
            "sector_factors": selected_sectors,
            "sector_components": sector_components[:2],
            "sector_reference_factors": _select_reference_factors(reference_candidates),
        }

    # =========================================================================
    # P0: MA/BOLL后端实时计算 + 内存缓存
    # =========================================================================

    async def _calc_technical_indicators(
        self,
        code: str,
        session: AsyncSession,
        target_date: date | None = None,
    ) -> dict:
        """从stock_kline计算技术指标(MA/BOLL/MACD/RSI/KDJ等)。

        缓存键包含调用方明确传入的K线截止日。盘中用途必须传入最近完整
        交易日，不能把仍在更新的当日 ``spot_fallback`` 日线缓存成收盘值。
        """
        trade_day = target_date or await resolve_latest_trade_date(
            session,
            StockKline.trade_date,
        )
        cache_date = trade_day.isoformat()
        cache_key = f"{code}:{cache_date}"
        if self._cache_date != cache_date:
            self._tech_cache.clear()
            self._cache_date = cache_date

        if cache_key in self._tech_cache:
            return self._tech_cache[cache_key]

        # 260个交易日覆盖约一年，仍按单股单日缓存，避免盘中重复查询。
        result = await session.execute(
            select(StockKline)
            .where(
                StockKline.code == code,
                StockKline.trade_date <= trade_day,
            )
            .order_by(desc(StockKline.trade_date))
            .limit(260)
        )
        klines = list(reversed(result.scalars().all()))  # 从远到近

        if len(klines) < 5:
            self._tech_cache[cache_key] = {}
            return {}

        closes = np.array([float(k.close) for k in klines if _safe_float(k.close) > 0], dtype=float)
        volumes = np.array([float(k.volume) for k in klines if _safe_float(k.volume) > 0], dtype=float)
        highs = np.array([float(k.high) for k in klines if _safe_float(k.high) > 0], dtype=float)
        lows = np.array([float(k.low) for k in klines if _safe_float(k.low) > 0], dtype=float)
        aligned_ohlc = [
            (float(k.high), float(k.low), float(k.close))
            for k in klines
            if _safe_float(k.high) > 0
            and _safe_float(k.low) > 0
            and _safe_float(k.close) > 0
        ]

        latest_kline = klines[-1]
        tech = {
            "trade_date": str(latest_kline.trade_date),
            "source": str(latest_kline.source or ""),
            "indicator_bar_count": len(closes),
        }

        # MA
        if len(closes) >= 5:
            tech["ma5"] = round(float(closes[-5:].mean()), 2)
        if len(closes) >= 10:
            tech["ma10"] = round(float(closes[-10:].mean()), 2)
        if len(closes) >= 20:
            tech["ma20"] = round(float(closes[-20:].mean()), 2)
            tech["return_20d"] = round(float((closes[-1] / closes[-20] - 1.0) * 100.0), 2)
        if len(closes) >= 60:
            tech["ma60"] = round(float(closes[-60:].mean()), 2)
        if len(closes) >= 25:
            ma20_current = float(closes[-20:].mean())
            ma20_five_days_ago = float(closes[-25:-5].mean())
            if ma20_five_days_ago > 0:
                tech["ma20_slope_5d"] = round(
                    float((ma20_current / ma20_five_days_ago - 1.0) * 100.0),
                    3,
                )

        # BOLL(20,2)
        if len(closes) >= 20:
            ma20 = closes[-20:].mean()
            std20 = closes[-20:].std()
            tech["boll_upper"] = round(float(ma20 + 2 * std20), 2)
            tech["boll_mid"] = round(float(ma20), 2)
            tech["boll_lower"] = round(float(ma20 - 2 * std20), 2)

        # MACD(12,26,9): 只在DIF/DEA最后两点都有效时判定交叉。
        if len(closes) >= 35:
            ema12 = _ema_series(closes, 12)
            ema26 = _ema_series(closes, 26)
            dif = ema12 - ema26
            valid_start = 25
            dif_valid = dif[valid_start:]
            dea_valid = _ema_series(dif_valid, 9)
            hist_valid = 2.0 * (dif_valid - dea_valid)
            finite_indices = np.flatnonzero(np.isfinite(hist_valid))
            if len(finite_indices) > 0:
                latest_index = int(finite_indices[-1])
                tech["macd_dif"] = round(float(dif_valid[latest_index]), 4)
                tech["macd_dea"] = round(float(dea_valid[latest_index]), 4)
                tech["macd_hist"] = round(float(hist_valid[latest_index]), 4)
            if len(finite_indices) >= 2:
                previous_hist = float(hist_valid[int(finite_indices[-2])])
                latest_hist = float(hist_valid[int(finite_indices[-1])])
                if latest_hist > 0 >= previous_hist:
                    tech["macd_signal"] = "golden_cross"
                elif latest_hist < 0 <= previous_hist:
                    tech["macd_signal"] = "death_cross"

        rsi14 = _latest_wilder_rsi(closes, 14)
        if rsi14 is not None:
            tech["rsi14"] = round(float(rsi14), 1)

        if len(aligned_ohlc) >= 9:
            aligned_highs = np.array([item[0] for item in aligned_ohlc], dtype=float)
            aligned_lows = np.array([item[1] for item in aligned_ohlc], dtype=float)
            aligned_closes = np.array([item[2] for item in aligned_ohlc], dtype=float)
            kdj = _kdj_series(aligned_highs, aligned_lows, aligned_closes)
            if kdj is not None:
                k_values, d_values, j_values = kdj
                finite_indices = np.flatnonzero(np.isfinite(k_values) & np.isfinite(d_values))
                if len(finite_indices) > 0:
                    latest_index = int(finite_indices[-1])
                    tech["kdj_k"] = round(float(k_values[latest_index]), 2)
                    tech["kdj_d"] = round(float(d_values[latest_index]), 2)
                    tech["kdj_j"] = round(float(j_values[latest_index]), 2)
                if len(finite_indices) >= 2:
                    previous_index = int(finite_indices[-2])
                    latest_index = int(finite_indices[-1])
                    previous_diff = float(k_values[previous_index] - d_values[previous_index])
                    latest_diff = float(k_values[latest_index] - d_values[latest_index])
                    if latest_diff > 5 and previous_diff <= 0:
                        tech["kdj_signal"] = "golden_cross"
                    elif latest_diff < -5 and previous_diff >= 0:
                        tech["kdj_signal"] = "death_cross"

        # N日最高价
        if len(highs) >= 20:
            tech["high_20d"] = round(float(highs[-20:].max()), 2)
        if len(highs) >= 60:
            tech["high_60d"] = round(float(highs[-60:].max()), 2)
        if len(highs) >= 120:
            tech["high_120d"] = round(float(highs[-120:].max()), 2)
        if len(highs) >= 250:
            tech["high_250d"] = round(float(highs[-250:].max()), 2)

        # 20日均量
        if len(volumes) >= 20:
            tech["avg_vol_20d"] = float(volumes[-20:].mean())

        # 近N日收盘价(用于盘整天数计算)
        if len(closes) >= 20:
            tech["recent_closes"] = [float(c) for c in closes[-20:]]

        # 重点观察池用前一交易日完整K线作基线，盘中价另行动态计算。
        baseline_klines = klines
        if klines and klines[-1].trade_date == trade_day:
            baseline_klines = klines[:-1]
        baseline_klines = [
            item
            for item in baseline_klines
            if _safe_float(item.close) > 0
            and _safe_float(item.high) > 0
            and _safe_float(item.low) > 0
        ]
        if len(baseline_klines) >= 60:
            profile_closes = [float(item.close) for item in baseline_klines[-260:]]
            profile_highs = [float(item.high) for item in baseline_klines[-260:]]
            profile_lows = [float(item.low) for item in baseline_klines[-260:]]
            profile_volumes = [float(item.volume or 0) for item in baseline_klines[-260:]]
            tech["low_base_profile"] = {
                "closes": profile_closes,
                "highs": profile_highs,
                "lows": profile_lows,
                "volumes": profile_volumes,
            }
            tech["long_cycle_profile"] = build_long_cycle_profile(
                closes=profile_closes,
                highs=profile_highs,
                lows=profile_lows,
                volumes=profile_volumes,
            )

        self._tech_cache[cache_key] = tech
        return tech

    # =========================================================================
    # P0: DragonHead板块成分股组装
    # =========================================================================

    async def _load_leader_history_features(
        self,
        session: AsyncSession,
        codes: list[str],
        trade_day: date,
    ) -> dict[str, dict]:
        """批量计算龙头低位程度，避免板块循环内逐股查K线。"""
        normalized_codes = list(dict.fromkeys(str(code or "") for code in codes if code))
        if not normalized_codes:
            return {}
        result = await session.stream(
            select(
                StockKline.code,
                StockKline.trade_date,
                StockKline.close,
                StockKline.high,
                StockKline.low,
            ).where(
                StockKline.code.in_(normalized_codes),
                StockKline.trade_date <= trade_day,
                StockKline.trade_date >= trade_day - timedelta(days=190),
            ).order_by(StockKline.code, StockKline.trade_date).execution_options(yield_per=2048)
        )
        grouped: dict[str, list[tuple]] = defaultdict(list)
        try:
            async for batch in result.partitions(2048):
                for code, trade_date, close, high, low in batch:
                    if _safe_float(close) > 0:
                        grouped[str(code)].append((trade_date, _safe_float(close), _safe_float(high), _safe_float(low)))
                await asyncio.sleep(0)
        finally:
            await result.close()

        features: dict[str, dict] = {}
        for index, (code, rows) in enumerate(grouped.items()):
            if index % 64 == 0:
                await asyncio.sleep(0)
            closes = [row[1] for row in rows]
            highs = [row[2] for row in rows if row[2] > 0]
            lows = [row[3] for row in rows if row[3] > 0]
            latest = closes[-1]
            return_20d = (latest / closes[-21] - 1.0) * 100.0 if len(closes) >= 21 and closes[-21] > 0 else 0.0
            range_high = max(highs[-120:] or [latest])
            range_low = min(lows[-120:] or [latest])
            range_high_20 = max(highs[-20:] or [latest])
            range_low_20 = min(lows[-20:] or [latest])
            position_120 = (
                (latest - range_low) / (range_high - range_low)
                if range_high > range_low
                else 0.5
            )
            position_20 = (
                (latest - range_low_20) / (range_high_20 - range_low_20)
                if range_high_20 > range_low_20
                else 0.5
            )
            ma5 = sum(closes[-5:]) / 5 if len(closes) >= 5 else 0.0
            ma10 = sum(closes[-10:]) / 10 if len(closes) >= 10 else 0.0
            ma20 = sum(closes[-20:]) / 20 if len(closes) >= 20 else 0.0
            ma20_previous = sum(closes[-25:-5]) / 20 if len(closes) >= 25 else 0.0
            ma20_slope_5d = (
                (ma20 / ma20_previous - 1.0) * 100.0
                if ma20 > 0 and ma20_previous > 0
                else 0.0
            )
            return_5d = (
                (latest / closes[-6] - 1.0) * 100.0
                if len(closes) >= 6 and closes[-6] > 0
                else 0.0
            )
            features[code] = {
                "return_20d": round(return_20d, 2),
                "return_5d": round(return_5d, 2),
                "position_120": round(max(0.0, min(position_120, 1.0)), 3),
                "position_20": round(max(0.0, min(position_20, 1.0)), 3),
                "ma5": round(ma5, 3),
                "ma10": round(ma10, 3),
                "ma20": round(ma20, 3),
                "ma20_slope_5d": round(ma20_slope_5d, 3),
            }
        return features

    async def _load_primary_industry_map(
        self,
        session: AsyncSession,
        codes: list[str],
    ) -> dict[str, str]:
        """加载同花顺主营行业，供核心业务相关性校验。"""
        normalized_codes = list(dict.fromkeys(str(code or "") for code in codes if code))
        if not normalized_codes:
            return {}
        result = await session.execute(
            select(StockSectorMapping.code, StockSectorMapping.sector_name).where(
                StockSectorMapping.code.in_(normalized_codes),
                StockSectorMapping.sector_type == "industry",
                StockSectorMapping.source == "pywencai",
            )
        )
        return {
            str(code): str(sector_name or "").strip()
            for code, sector_name in result.all()
            if code and str(sector_name or "").strip()
        }

    async def _assemble_sector_stocks(
        self,
        session: AsyncSession,
        sector_code: str,
        target_date: date | None = None,
        catalyst_map: dict[str, dict] | None = None,
        *, as_of_at: datetime | None = None,
    ) -> list[dict]:
        """组装板块成分股 → DragonHeadScanner.scan_sector() 输入

        三表JOIN: board_cons → stock_spot → limit_up_pool
        输出: [{code, name, change_pct, main_net_inflow, consecutive_days, turnover, seal_amount}]
        """
        fund_decision_at = as_of_at or datetime.now()
        # 1. 板块成分股: 优先 board_cons，开发库为空时回退到已验证的个股板块映射。
        result = await session.execute(
            select(BoardCons.code, BoardCons.name)
            .where(BoardCons.sector_code == sector_code)
        )
        cons = {r[0]: r[1] for r in result.all()}
        if not cons:
            mapping_result = await session.execute(
                select(StockSectorMapping.code)
                .where(StockSectorMapping.sector_code == sector_code)
            )
            cons = {code: code for code, in mapping_result.all()}

        if not cons:
            return []

        # 2. 实时行情
        result = await session.execute(
            select(StockSpot).where(StockSpot.code.in_(cons.keys()))
        )
        spots = {s.code: s for s in result.scalars().all()}

        today = target_date or fund_decision_at.date()
        fund_status: dict[str, str] = {}
        current_fund_map = await load_current_main_fund_map(
            session, trade_date=today, decision_at=fund_decision_at,
            codes=cons.keys(), diagnostics=fund_status,
        )
        # Dated display evidence is never a substitute for current scoring input.
        fund_display = await load_latest_main_fund_display_map(
            session, trade_date=today, as_of_at=fund_decision_at, codes=cons.keys(),
        )

        # 3. 今日涨停池
        result = await session.execute(
            select(LimitUpPool).where(
                LimitUpPool.trade_date == today,
                LimitUpPool.code.in_(cons.keys()),
            )
        )
        limit_ups = {lu.code: lu for lu in result.scalars().all()}
        history_features = await self._load_leader_history_features(
            session,
            list(cons.keys()),
            today,
        )
        primary_industry_map = await self._load_primary_industry_map(
            session,
            list(cons.keys()),
        )

        # 4. 组装
        stocks = []
        for code, name in cons.items():
            spot = spots.get(code)
            lu = limit_ups.get(code)
            catalyst = (catalyst_map or {}).get(code) or {}
            is_one_word = bool(
                lu
                and spot
                and _safe_float(spot.open) >= _safe_float(lu.limit_up_price or spot.limit_up) * 0.999
            )

            stocks.append({
                "code": code,
                "name": spot.name if spot and spot.name else (lu.name if lu and lu.name else name),
                "change_pct": spot.change_pct if spot else 0,
                "main_net_inflow": (current_fund_map.get(code) or {}).get("main_net_inflow"),
                "main_fund_status": fund_status.get(code, "missing"),
                "main_fund_decision_at": fund_decision_at.isoformat(),
                "main_fund_display": fund_display.get(code),
                "consecutive_days": lu.consecutive_days if lu else 1,
                "turnover": spot.turnover if spot else 0,
                "seal_amount": lu.seal_amount if lu else 0,
                "volume_ratio": spot.volume_ratio if spot else 1.0,
                "seal_quality_score": spot.seal_quality_score if spot else 0,
                "price": spot.price if spot else 0,
                "avg_price": spot.avg_price if spot else 0,
                "amount": spot.amount if spot else 0,
                "is_limit_up": bool(lu),
                "limit_up_time": lu.limit_up_time if lu else "",
                "limit_up_reason": str(lu.limit_up_reason or "") if lu else "",
                "break_count": int(lu.break_count or 0) if lu else 0,
                "is_one_word": is_one_word,
                "event_grade": str(catalyst.get("news_event_grade") or ""),
                "event_score": _safe_float(catalyst.get("news_catalyst_score")),
                "news_evidence": catalyst.get("news_evidence") or {},
                "primary_industry": primary_industry_map.get(code, ""),
                **(history_features.get(code) or {}),
            })

        return stocks

    async def _assemble_many_sector_stocks(
        self,
        session: AsyncSession,
        sector_codes: list[str],
        target_date: date | None = None,
        catalyst_map: dict[str, dict] | None = None,
        *, as_of_at: datetime | None = None,
    ) -> dict[str, list[dict]]:
        """批量组装多个板块成分股，避免龙头追踪按板块重复查库。"""
        fund_decision_at = as_of_at or datetime.now()
        unique_sector_codes = list(dict.fromkeys(code for code in sector_codes if code))
        if not unique_sector_codes:
            return {}

        board_result = await session.execute(
            select(BoardCons.sector_code, BoardCons.code, BoardCons.name)
            .where(BoardCons.sector_code.in_(unique_sector_codes))
        )
        cons_by_sector: dict[str, dict[str, str]] = defaultdict(dict)
        for sector_code, code, name in board_result.all():
            if sector_code and code:
                cons_by_sector[sector_code][code] = name or code

        missing_sector_codes = [
            sector_code
            for sector_code in unique_sector_codes
            if not cons_by_sector.get(sector_code)
        ]
        if missing_sector_codes:
            mapping_result = await session.execute(
                select(StockSectorMapping.sector_code, StockSectorMapping.code)
                .where(StockSectorMapping.sector_code.in_(missing_sector_codes))
            )
            for sector_code, code in mapping_result.all():
                if sector_code and code:
                    cons_by_sector[sector_code][code] = code

        all_codes = sorted({
            code
            for cons in cons_by_sector.values()
            for code in cons.keys()
        })
        if not all_codes:
            return {sector_code: [] for sector_code in unique_sector_codes}

        spot_result = await session.execute(
            select(StockSpot).where(StockSpot.code.in_(all_codes))
        )
        spots = {spot.code: spot for spot in spot_result.scalars().all()}

        trade_day = target_date or fund_decision_at.date()
        fund_status: dict[str, str] = {}
        current_fund_map = await load_current_main_fund_map(
            session, trade_date=trade_day, decision_at=fund_decision_at,
            codes=all_codes, diagnostics=fund_status,
        )
        fund_display = await load_latest_main_fund_display_map(
            session, trade_date=trade_day, as_of_at=fund_decision_at, codes=all_codes,
        )

        limit_up_result = await session.execute(
            select(LimitUpPool).where(
                LimitUpPool.trade_date == trade_day,
                LimitUpPool.code.in_(all_codes),
            )
        )
        limit_ups = {lu.code: lu for lu in limit_up_result.scalars().all()}
        history_features = await self._load_leader_history_features(
            session,
            all_codes,
            trade_day,
        )
        primary_industry_map = await self._load_primary_industry_map(
            session,
            all_codes,
        )

        stocks_by_sector: dict[str, list[dict]] = {}
        for sector_code in unique_sector_codes:
            await asyncio.sleep(0)
            stocks = []
            for code, name in cons_by_sector.get(sector_code, {}).items():
                spot = spots.get(code)
                lu = limit_ups.get(code)
                catalyst = (catalyst_map or {}).get(code) or {}
                is_one_word = bool(
                    lu
                    and spot
                    and _safe_float(spot.open) >= _safe_float(lu.limit_up_price or spot.limit_up) * 0.999
                )
                stocks.append({
                    "code": code,
                    "name": spot.name if spot and spot.name else (lu.name if lu and lu.name else name),
                    "change_pct": spot.change_pct if spot else 0,
                    "main_net_inflow": (current_fund_map.get(code) or {}).get("main_net_inflow"),
                    "main_fund_status": fund_status.get(code, "missing"),
                    "main_fund_decision_at": fund_decision_at.isoformat(),
                    "main_fund_display": fund_display.get(code),
                    "consecutive_days": lu.consecutive_days if lu else 1,
                    "turnover": spot.turnover if spot else 0,
                    "seal_amount": lu.seal_amount if lu else 0,
                    "volume_ratio": spot.volume_ratio if spot else 1.0,
                    "seal_quality_score": spot.seal_quality_score if spot else 0,
                    "price": spot.price if spot else 0,
                    "avg_price": spot.avg_price if spot else 0,
                    "amount": spot.amount if spot else 0,
                    "is_limit_up": bool(lu),
                    "limit_up_time": lu.limit_up_time if lu else "",
                    "limit_up_reason": str(lu.limit_up_reason or "") if lu else "",
                    "break_count": int(lu.break_count or 0) if lu else 0,
                    "is_one_word": is_one_word,
                    "event_grade": str(catalyst.get("news_event_grade") or ""),
                    "event_score": _safe_float(catalyst.get("news_catalyst_score")),
                    "news_evidence": catalyst.get("news_evidence") or {},
                    "primary_industry": primary_industry_map.get(code, ""),
                    **(history_features.get(code) or {}),
                })
            stocks_by_sector[sector_code] = stocks

        return stocks_by_sector

    # =========================================================================
    # P1: BullScore连板评分链路
    # =========================================================================

    async def _get_promotion_result(
        self,
        code: str,
        session: AsyncSession,
        target_date: date | None = None,
    ) -> Optional[PromotionResult]:
        """获取晋级概率 — 从LimitUpPool→LimitUpTracker.predict_promotion()"""
        today = target_date or date.today()
        result = await session.execute(
            select(LimitUpPool).where(
                LimitUpPool.code == code,
                LimitUpPool.trade_date == today,
            )
        )
        lu = result.scalar_one_or_none()
        if not lu:
            return None

        return self.limit_up_tracker.predict_promotion(
            code=code,
            name=lu.name,
            current_days=lu.consecutive_days or 1,
            stock_data={
                "seal_amount": lu.seal_amount or 0,
                "break_count": lu.break_count or 0,
                "turnover": lu.turnover or 0,
            },
        )

    # =========================================================================
    # P1: 竞价→明日预案
    # =========================================================================

    async def _generate_next_day_plan(
        self,
        code: str,
        bull_result: BullScoreResult,
        session: AsyncSession,
        target_date: date | None = None,
    ) -> Optional[dict]:
        """生成明日购买预案

        输入: BullScoreResult + AuctionData + 技术位 + 板块Lifecycle
        输出: {strategy, support, resistance, invalidation, notes}
        """
        # 1. 竞价数据
        today = target_date or date.today()
        result = await session.execute(
            select(AuctionData).where(
                AuctionData.code == code,
                AuctionData.trade_date == today,
            ).order_by(*auction_latest_order()).limit(1)
        )
        auction = result.scalar_one_or_none()

        # 2. 技术位
        tech = await self._calc_technical_indicators(code, session, target_date=today)

        # 3. 板块Lifecycle
        result = await session.execute(
            select(StockSectorMapping.sector_code)
            .where(StockSectorMapping.code == code)
            .limit(3)
        )
        sector_codes = [r[0] for r in result.all()]

        lifecycle_state = None
        if sector_codes:
            result = await session.execute(
                select(SectorPersistence).where(
                    SectorPersistence.sector_code.in_(sector_codes),
                    SectorPersistence.trade_date == today,
                ).order_by(desc(SectorPersistence.strength_score))
            )
            sp = result.scalar_one_or_none()
            lifecycle_state = sp  # 有或无

        # 4. 策略判定
        strategy = "normal"
        auction_status = auction_evidence_status(auction)
        if auction and auction_status == "ok" and not auction.is_cancelled:
            if auction.open_change and auction.open_change > 3 and auction.volume_ratio and auction.volume_ratio > 2:
                strategy = "auction_follow"  # 竞价强势跟
            elif auction.is_cancelled:
                strategy = "cancel"  # 竞价撤单, 观望

        # 5. 支撑/压力位
        support = tech.get("ma20", 0) or tech.get("ma60", 0)
        resistance = tech.get("high_20d", 0) or tech.get("boll_upper", 0)

        # 6. 失效条件
        invalidation = f"跌破支撑位{support:.2f}" if support > 0 else "跌破MA20"

        # 7. 综合建议
        level = bull_result.level
        if level in ("S", "A"):
            action = "重点关注, 竞价高开可跟进" if auction_status == "ok" else "重点观察, 竞价证据未验证"
        elif level == "B":
            action = "观察为主, 突破确认后介入"
        else:
            action = "暂不建议, 等待信号增强"

        notes = []
        if lifecycle_state:
            notes.append(f"板块状态: {lifecycle_state}")
        if bull_result.risk_warnings:
            notes.extend(bull_result.risk_warnings)

        return {
            "code": code,
            "strategy": strategy,
            "action": action,
            "auction_evidence_status": auction_status,
            "support": round(support, 2) if support else None,
            "resistance": round(resistance, 2) if resistance else None,
            "invalidation": invalidation,
            "bull_level": level,
            "notes": notes[:5],
        }

    # =========================================================================
    # 核心扫描: 全市场异动检测
    # =========================================================================

    @staticmethod
    def _current_fund_evidence(raw: dict | None, trade_date: date, decision_at: datetime) -> dict:
        item = dict(raw or {})
        status = fund_clock_status(
            item.get("source_quote_at"), item.get("received_at"), item.get("observed_at"),
            trade_date, decision_at, settings.FUND_FLOW_SOURCE_MAX_AGE_SEC,
        )
        provider = item.get("provider_source") or item.get("source")
        if not main_fund_source_supported(provider, item.get("source_version")):
            status = "unsupported_source"
        item["clock_status"] = status
        item["is_stale"] = bool(
            item.get("is_stale") or status != "ok"
            or not main_fund_values_valid(item.get("main_net_inflow"), item.get("main_net_inflow_pct"))
        )
        source_at = evidence_clock(item.get("source_quote_at"))
        item["as_of"] = source_at.isoformat() if source_at else None
        return item

    async def scan_market(
        self,
        session: AsyncSession,
        min_score: float = 50,
        target_date: date | None = None,
        current_fund_map: dict[str, dict] | None = None,
        recent_funds_map: dict[str, list[dict]] | None = None,
        fund_trade_date: date | None = None,
        fund_snapshot_is_stale: bool = False,
        fund_source: str = "eastmoney_main_fund",
        as_of_at: datetime | None = None,
    ) -> list[AnomalyEvent]:
        """全市场异动扫描 — 核心入口

        流程: stock_spot全量 → 逐只检测 → 异动事件列表
        """
        decision_at = as_of_at if as_of_at is not None else datetime.now()
        # 1. 获取全量实时行情
        result = await session.execute(
            select(StockSpot).where(StockSpot.price > 0)
        )
        spots = result.scalars().all()

        if not spots:
            return []

        # 2. 获取今日涨停/跌停池(用于快速判断)
        today = target_date or date.today()
        if today == date.today():
            # 单表upsert会保留停牌、ST和退市票的旧快照；它们既不能参与当日信号，
            # 也不能进入下面的连续快照状态机，否则会反向清空当日价格/成交轨迹。
            spots = [
                spot
                for spot in spots
                if isinstance(getattr(spot, "updated_at", None), datetime)
                and spot.updated_at.date() == today
            ]
            latest_quote_at = max(
                (spot.updated_at for spot in spots),
                default=None,
            )
            if latest_quote_at is not None:
                round_cutoff = latest_quote_at - timedelta(
                    seconds=max(1, settings.ANOMALY_QUOTE_ROUND_TOLERANCE_SEC)
                )
                spots = [spot for spot in spots if spot.updated_at >= round_cutoff]
                current_session = trade_calendar.get_trade_session()
                if current_session in {"pre_auction", "morning", "afternoon"}:
                    freshness_now = datetime.now()
                    stale_count = sum(
                        1
                        for spot in spots
                        if not _spot_is_fresh_for_live_scan(
                            spot,
                            now=freshness_now,
                            trade_date=today,
                        )
                    )
                    spots = [
                        spot
                        for spot in spots
                        if _spot_is_fresh_for_live_scan(
                            spot,
                            now=freshness_now,
                            trade_date=today,
                        )
                    ]
                    if stale_count:
                        logger.warning(
                            "异动扫描剔除源行情陈旧快照: "
                            f"stale={stale_count}, fresh={len(spots)}"
                        )
        if not spots:
            return []
        spot_by_code = {str(spot.code or ""): spot for spot in spots}
        lu_result = await session.execute(
            select(LimitUpPool).where(LimitUpPool.trade_date == today)
        )
        limit_up_map = {lu.code: lu for lu in lu_result.scalars().all()}

        # 2.1 获取个股 -> 板块映射 + 今日板块持续性，用于卡片展示板块因子/成分股
        all_codes = [spot.code for spot in spots]
        mapping_result = await session.execute(
            select(
                StockSectorMapping.code,
                StockSectorMapping.sector_code,
                StockSectorMapping.sector_name,
                StockSectorMapping.sector_type,
                StockSectorMapping.source,
            )
            .where(StockSectorMapping.code.in_(all_codes))
        )
        code_sector_map: dict[str, list[dict]] = {}
        all_sector_codes: set[str] = set()
        raw_sector_rows = mapping_result.all()
        all_sector_codes.update(sector_code for _, sector_code, _, _, _ in raw_sector_rows if sector_code)

        excluded_sector_codes: set[str] = set()
        if all_sector_codes:
            sector_info_result = await session.execute(
                select(SectorInfo.sector_code).where(
                    SectorInfo.sector_code.in_(all_sector_codes),
                    SectorInfo.is_excluded == 1,
                )
            )
            excluded_sector_codes = {row[0] for row in sector_info_result.all()}

        for stock_code, sector_code, sector_name, sector_type, mapping_source in raw_sector_rows:
            if not sector_code:
                continue
            if sector_code in excluded_sector_codes:
                continue
            if sector_type and sector_type not in {"concept", "industry"}:
                continue
            code_sector_map.setdefault(stock_code, []).append(
                {
                    "sector_code": sector_code,
                    "sector_name": sector_name or sector_code,
                    "sector_type": sector_type or "",
                    "source": mapping_source or "",
                }
            )

        sector_persistence_map: dict[str, SectorPersistence] = {}
        if all_sector_codes:
            sp_result = await session.execute(
                select(SectorPersistence).where(
                    SectorPersistence.trade_date == today,
                    SectorPersistence.sector_code.in_(all_sector_codes),
                )
            )
            sector_persistence_map = {
                sp.sector_code: sp for sp in sp_result.scalars().all()
            }

        sector_lifecycle_map: dict[str, SectorLifecycle] = {}
        current_sector_leader_codes: dict[str, set[str]] = defaultdict(set)
        main_wave_pool_codes = {
            str(code)
            for code, setup in self.dynamic_trend_pool.items()
            if str((setup or {}).get("source") or "")
            == "main_wave_pullback_pattern"
        }
        main_wave_sector_codes = {
            str(item.get("sector_code") or "")
            for code in main_wave_pool_codes
            for item in (code_sector_map.get(code) or [])
            if item.get("sector_code")
        }
        if main_wave_sector_codes:
            lifecycle_result = await session.execute(
                select(SectorLifecycle).where(
                    SectorLifecycle.trade_date == today,
                    SectorLifecycle.sector_code.in_(main_wave_sector_codes),
                    SectorLifecycle.lifecycle_state.in_(
                        tuple(MAIN_WAVE_CORE_LIFECYCLE_STATES)
                    ),
                )
            )
            for lifecycle in lifecycle_result.scalars().all():
                sector_code = str(lifecycle.sector_code or "")
                sector_lifecycle_map[sector_code] = lifecycle
                try:
                    leaders = json.loads(lifecycle.leader_stocks or "[]")
                except (TypeError, ValueError, json.JSONDecodeError):
                    leaders = []
                for leader in leaders if isinstance(leaders, list) else []:
                    leader_code = str((leader or {}).get("code") or "")
                    if leader_code:
                        current_sector_leader_codes[sector_code].add(leader_code)
            recent_leader_result = await session.execute(
                select(
                    SectorLifecycle.sector_code,
                    SectorLifecycle.leader_stocks,
                ).where(
                    SectorLifecycle.trade_date >= today - timedelta(days=14),
                    SectorLifecycle.trade_date <= today,
                    SectorLifecycle.sector_code.in_(main_wave_sector_codes),
                )
            )
            for sector_code, raw_leaders in recent_leader_result.all():
                try:
                    leaders = json.loads(raw_leaders or "[]")
                except (TypeError, ValueError, json.JSONDecodeError):
                    leaders = []
                for leader in leaders if isinstance(leaders, list) else []:
                    leader_code = str((leader or {}).get("code") or "")
                    if leader_code:
                        current_sector_leader_codes[str(sector_code)].add(leader_code)

        sector_limitup_components: dict[str, list[dict]] = {}
        for stock_code, sectors in code_sector_map.items():
            lu = limit_up_map.get(stock_code)
            if not lu:
                continue
            for sector in sectors:
                sector_limitup_components.setdefault(sector["sector_code"], []).append(
                    {
                        "code": stock_code,
                        "name": lu.name or stock_code,
                        "consecutive_days": int(lu.consecutive_days or 1),
                        "label": (
                            f"{lu.name or stock_code}({int(lu.consecutive_days or 1)}连板)"
                            if int(lu.consecutive_days or 1) > 1
                            else f"{lu.name or stock_code}(首板)"
                        ),
                    }
                )
        for sector_code, peers in sector_limitup_components.items():
            peers.sort(
                key=lambda item: (
                    -int(item.get("consecutive_days") or 0),
                    str(item.get("name") or ""),
                )
            )

        market_repair_context = self._build_market_repair_context(
            spots,
            len(limit_up_map),
        )

        # 3. 获取次新股标记
        tag_result = await session.execute(
            select(
                StockTag.code,
                StockTag.is_ipo_recent,
                StockTag.is_st,
                StockTag.is_suspended,
                StockTag.is_delisting,
            )
        )
        tag_rows = tag_result.all()
        ipo_recent_set = {row[0] for row in tag_rows if bool(row[1])}
        blocked_stock_set = {
            row[0]
            for row in tag_rows
            if bool(row[2]) or bool(row[3]) or bool(row[4])
        }
        if today == date.today():
            drained = self._drain_quote_batch_inbox(
                blocked_stock_set=blocked_stock_set,
                today=today,
                allowed_codes=set(spot_by_code),
            )
            if drained:
                logger.debug(f"异动连续状态机回放已提交行情: {drained} 条")

        # 4. 获取最新可用资金流日期 + 近5日资金流，供连续净流检测使用
        latest_fund_trade_date = fund_trade_date
        injected_current_funds = current_fund_map is not None
        if latest_fund_trade_date is None:
            latest_fund_trade_date = await session.scalar(
                select(func.max(FundFlow.trade_date)).where(FundFlow.trade_date <= today)
            )

        if current_fund_map is None or (injected_current_funds and not current_fund_map):
            qualified = await load_current_main_fund_map(
                session, trade_date=today, decision_at=decision_at,
            )
            # Historical rows stay in recent_funds_map for dated research, never
            # promoted into current evidence by an empty-cache fallback.
            current_fund_map = {
                code: {**row, "provider_source": row["source"],
                       "source": "fund_flow", "as_of": row["source_quote_at"].isoformat()}
                for code, row in qualified.items()
            }
            fund_snapshot_is_stale = not bool(current_fund_map)

        recent_fund_window = None
        if recent_funds_map is None or recent_funds_map:
            # A caller's dated amount list is only a hint, not source-clock or
            # calendar proof. Revalidate non-empty hints from the same authority
            # as rank/plan; an explicit {} remains unavailable without fallback.
            recent_fund_window = await load_main_fund_window(
                session, through_date=today, decision_at=decision_at, codes=all_codes,
            )
            recent_funds_map = {
                code: list(reversed(item["history"])) if item["complete"] else []
                for code, item in recent_fund_window["items"].items()
            }
            # Missing/invalid sessions must not become a shorter continuous run,
            # a partial sum, or measured zero inside the legacy detector.

        recent_limitup_result = await session.execute(
            select(
                LimitUpPool.code,
                LimitUpPool.trade_date,
                LimitUpPool.consecutive_days,
                LimitUpPool.break_count,
            ).where(
                LimitUpPool.code.in_(all_codes),
                LimitUpPool.trade_date >= today - timedelta(days=10),
                LimitUpPool.trade_date <= today,
            )
        )
        recent_limitup_meta: dict[str, dict] = {}
        for code, trade_date, consecutive_days, break_count in recent_limitup_result.all():
            meta = recent_limitup_meta.setdefault(
                code,
                {
                    "max_recent_consecutive_days": 0,
                    "recent_limit_up_hits": 0,
                    "last_limit_up_date": None,
                    "last_break_count": 0,
                },
            )
            meta["max_recent_consecutive_days"] = max(
                int(meta.get("max_recent_consecutive_days") or 0),
                int(consecutive_days or 0),
            )
            meta["recent_limit_up_hits"] = int(meta.get("recent_limit_up_hits") or 0) + 1
            if meta["last_limit_up_date"] is None or trade_date > meta["last_limit_up_date"]:
                meta["last_limit_up_date"] = trade_date
                meta["last_break_count"] = int(break_count or 0)

        events = []
        for spot in spots:
            code = spot.code
            tech: dict | None = None
            # 风险屏蔽必须早于日内状态跟踪，避免无效股票污染价格和成交历史。
            if spot.price <= 0:
                continue
            if code in blocked_stock_set or "ST" in str(spot.name or "").upper():
                continue
            # SQLAlchemy ORM对象上的瞬时属性不会回写数据库，只供本轮扫描复用。
            # 历史回放没有连续实时快照，只保留旧库中位于合理尺度内的值；
            # 当日实时扫描一律使用本进程连续快照，彻底隔离错误的腾讯[62]。
            spot._effective_min5_change = (
                self._track_intraday_min5_change(spot)
                if today == date.today()
                else _validated_min5(getattr(spot, "min5_change", None))
            )
            spot._intraday_amount_flow = (
                self._track_intraday_amount_flow(spot)
                if today == date.today()
                else {
                    "intraday_amount_confirmed": False,
                    "intraday_amount_delta": 0.0,
                    "intraday_amount_interval_sec": 0.0,
                    "intraday_amount_pace_ratio": 0.0,
                }
            )
            spot._rolling_60s_momentum = (
                self._track_rolling_60s_momentum(spot)
                if today == date.today()
                else {
                    "rolling_60s_confirmed": False,
                    "rolling_60s_change_pct": 0.0,
                    "rolling_60s_interval_sec": 0.0,
                    "rolling_60s_amount_delta": 0.0,
                    "rolling_60s_amount_pace_ratio": 0.0,
                    "rolling_60s_amount_reference": "",
                    "rolling_60s_close_position": 0.0,
                    "rolling_60s_peak_pullback_pct": 0.0,
                    "rolling_60s_up_leg_ratio": 0.0,
                    "rolling_60s_path_confirmed": False,
                    "rolling_60s_tier": "",
                }
            )
            reversal_state = (
                self._track_intraday_reversal_state(spot)
                if today == date.today()
                else {}
            )

            is_ipo = code in ipo_recent_set
            lu = limit_up_map.get(code)
            # DB fallback and injected caches share the same gate. Caller flags
            # and trade_date alone do not prove current funds were known then.
            current_fund = self._current_fund_evidence(
                current_fund_map.get(code), today, decision_at,
            )
            sector_context = self._build_sector_context_detail(
                code,
                code_sector_map,
                sector_persistence_map,
                sector_limitup_components,
                sector_lifecycle_map,
                current_sector_leader_codes,
            )
            board_context = recent_limitup_meta.get(code) or {}
            current_main_inflow = float(current_fund.get("main_net_inflow", 0) or 0)
            current_main_inflow_pct = float(current_fund.get("main_net_inflow_pct", 0) or 0)
            recent_funds = recent_funds_map.get(code, [])

            # --- 涨停检测 ---
            if spot.price > 0 and spot.limit_up > 0 and spot.price >= spot.limit_up:
                is_one_word = (spot.open >= spot.limit_up)  # 一字板: 开盘=涨停
                # 缩量涨停降级
                level = "critical"
                event_desc = "涨停"
                score = 85
                if spot.volume_ratio and spot.volume_ratio < 0.8:
                    level = "major"
                    event_desc += "(缩量⚠️)"
                    score = 65
                if is_one_word:
                    event_desc += "(一字板)"
                if lu:
                    if lu.consecutive_days and lu.consecutive_days >= 3:
                        event_desc = f"{lu.consecutive_days}连板" + event_desc[2:]
                        score = min(100, score + 10)

                tech = await self._calc_technical_indicators(code, session, target_date=today)
                events.append(AnomalyEvent(
                    code=code, name=spot.name,
                    event_type="limit_up", level=level, score=score,
                    detail={
                        **self._build_quote_detail(spot),
                        "seal_amount": lu.seal_amount if lu else 0,
                        "consecutive_days": lu.consecutive_days if lu else 1,
                        **self._build_orderbook_detail(spot),
                        **_derive_kline_boundary(tech or {}, spot.price or 0),
                        **sector_context,
                    },
                    description=event_desc,
                    is_ipo_recent=is_ipo,
                    is_one_word_board=is_one_word,
                ))

            # --- 跌停检测 ---
            if spot.price > 0 and spot.limit_down > 0 and spot.price <= spot.limit_down:
                events.append(AnomalyEvent(
                    code=code, name=spot.name,
                    event_type="limit_down", level="critical", score=80,
                    detail={
                        **self._build_quote_detail(spot),
                        **self._build_orderbook_detail(spot),
                        **sector_context,
                    },
                    description="跌停",
                    is_ipo_recent=is_ipo,
                ))

            # --- 资金异动(FundFlow优先 + 连续净流) ---
            if not current_fund.get("is_stale", True) and self._should_scan_capital(
                current_main_inflow=current_main_inflow,
                current_main_inflow_pct=current_main_inflow_pct,
                recent_funds=recent_funds,
                volume_ratio=_numeric_or_default(spot.volume_ratio, 1.0),
                change_pct=spot.change_pct or 0,
            ):
                anomalies = self.capital_detector.detect(
                    code=code, name=spot.name,
                    fund_data={
                        "main_net_inflow": current_main_inflow,
                        "main_net_inflow_pct": current_main_inflow_pct,
                        # Same qualified frame as event details. Do not drop the
                        # measured super-order leg or turn unknown components to 0.
                        **fund_order_breakdown(current_fund),
                    },
                    recent_funds=recent_funds,
                    volume_ratio=_numeric_or_default(spot.volume_ratio, 1.0),
                    change_pct=spot.change_pct or 0,
                    open_price=spot.open or 0,
                    high_price=spot.high or 0,
                    low_price=spot.low or 0,
                    close_price=spot.price,
                    volume=spot.volume or 0,
                )
                tech = await self._calc_technical_indicators(code, session, target_date=today)
                for a in anomalies:
                    if a.anomaly_type not in {"main_inflow", "consecutive", "pump_dump"}:
                        continue

                    if a.anomaly_type == "main_inflow":
                        qualifies = (
                            abs(current_main_inflow) >= self.capital_detector.MAIN_INFLOW_MAJOR
                            or (abs(current_main_inflow) >= self.capital_detector.MAIN_INFLOW_MINOR and abs(current_main_inflow_pct) >= 8)
                        )
                        if not qualifies or a.score < max(min_score, 70):
                            continue
                    elif a.anomaly_type == "consecutive" and a.score < max(min_score, 60):
                        continue
                    elif a.anomaly_type == "pump_dump" and a.score < max(min_score, 70):
                        continue

                    if a.score >= min_score:
                        current_fund_source = str(current_fund.get("source") or fund_source or "")
                        current_fund_is_stale = bool(current_fund.get("is_stale")) or bool(fund_snapshot_is_stale)
                        detail = {
                            **a.detail,
                            "capital_anomaly_type": a.anomaly_type,
                            **(fund_window_payload(recent_fund_window, code)
                               if recent_fund_window is not None else {}),
                            "main_net_inflow": current_main_inflow,
                            "source": current_fund_source if code in current_fund_map else "",
                            "as_of": str(current_fund.get("as_of") or (str(latest_fund_trade_date) if latest_fund_trade_date else str(today))),
                            "is_stale": bool(code in current_fund_map and current_fund_is_stale),
                            "main_net_inflow_pct": current_main_inflow_pct,
                            **fund_order_breakdown(current_fund),
                            **self._build_quote_detail(spot),
                            **self._build_orderbook_detail(spot),
                            **_derive_kline_boundary(tech or {}, spot.price or 0),
                            **sector_context,
                            **board_context,
                        }
                        anomaly_event_type = "pump_dump" if a.anomaly_type == "pump_dump" else "capital"
                        events.append(AnomalyEvent(
                            code=code, name=spot.name,
                            event_type=anomaly_event_type, level=a.level, score=a.score,
                            detail=detail, description=a.description,
                            is_ipo_recent=is_ipo,
                        ))

            # --- 低吸弱转强: 绿盘转强/上升通道回踩MA5 ---
            low_absorb_prescreen = (
                lu is None
                and -3.5 <= (spot.change_pct or 0) <= 2.8
                and 0.75 <= (_numeric_or_default(spot.volume_ratio, 1.0)) <= 2.6
                and (
                    _spot_effective_min5_change(spot) >= 0.25
                    or _safe_float(getattr(spot, "support_strength_score", 0)) >= 60
                    or current_main_inflow > 0
                )
            )
            if low_absorb_prescreen:
                tech = await self._calc_technical_indicators(code, session, target_date=today)
                if tech:
                    low_absorb = self._detect_low_absorb_setup(
                        spot=spot,
                        tech=tech,
                        current_fund=current_fund,
                        sector_context=sector_context,
                        board_context=board_context,
                        today=today,
                        latest_fund_trade_date=latest_fund_trade_date,
                        fund_snapshot_is_stale=fund_snapshot_is_stale,
                        fund_source=fund_source,
                    )
                    if low_absorb:
                        low_absorb_score, low_absorb_label, low_absorb_detail = low_absorb
                        if low_absorb_score >= min_score:
                            events.append(AnomalyEvent(
                                code=code, name=spot.name,
                                event_type="low_absorb",
                                level="major" if low_absorb_score >= 75 else "minor",
                                score=low_absorb_score,
                                detail=low_absorb_detail,
                                description=low_absorb_label,
                                is_ipo_recent=is_ipo,
                            ))

            # --- 水下快速反转: 不等涨停，翻红并站回VWAP时提前进入异动买点 ---
            if lu is None:
                underwater_reversal = self._detect_underwater_reversal_setup(
                    spot=spot,
                    current_fund=current_fund,
                    sector_context=sector_context,
                    board_context=board_context,
                    reversal_state=reversal_state,
                    today=today,
                    fund_snapshot_is_stale=fund_snapshot_is_stale,
                    fund_source=fund_source,
                )
                if underwater_reversal:
                    reversal_score, reversal_label, reversal_detail = underwater_reversal
                    if reversal_score >= min_score:
                        existing_low_absorb = [
                            item
                            for item in events
                            if item.code == code and item.event_type == "low_absorb"
                        ]
                        if (
                            not existing_low_absorb
                            or reversal_score >= max(item.score for item in existing_low_absorb)
                        ):
                            events[:] = [
                                item
                                for item in events
                                if not (item.code == code and item.event_type == "low_absorb")
                            ]
                            events.append(AnomalyEvent(
                                code=code,
                                name=spot.name,
                                event_type="low_absorb",
                                level="major" if reversal_score >= 78 else "minor",
                                score=reversal_score,
                                detail=reversal_detail,
                                description=reversal_label,
                                is_ipo_recent=is_ipo,
                            ))

            # --- 突破信号(放宽预筛: 不再死卡涨幅>3%) ---
            should_scan_breakthrough = (
                (spot.change_pct or 0) >= 1.5
                or _spot_effective_min5_change(spot) >= 1.0
                or bool(
                    (getattr(spot, "_rolling_60s_momentum", {}) or {}).get(
                        "rolling_60s_confirmed"
                    )
                )
                or (spot.volume_ratio or 0) >= 1.8
                or (
                    (spot.change_pct or 0) >= 2.0
                    and _spot_effective_min5_change(spot) >= 0.8
                    and (spot.volume_ratio or 0) >= 1.3
                )
            )
            if should_scan_breakthrough:
                tech = await self._calc_technical_indicators(code, session, target_date=today)
                if tech:
                    signals = self.breakthrough_detector.detect(
                        code=code, name=spot.name,
                        price=spot.price,
                        volume=_spot_volume_in_kline_unit(spot.volume),
                        high_20d=tech.get("high_20d", 0),
                        high_60d=tech.get("high_60d", 0),
                        high_120d=tech.get("high_120d", 0),
                        ma5=tech.get("ma5", 0),
                        ma10=tech.get("ma10", 0),
                        ma20=tech.get("ma20", 0),
                        ma60=tech.get("ma60", 0),
                        boll_upper=tech.get("boll_upper", 0),
                        avg_volume_20d=tech.get("avg_vol_20d", 0),
                        open_price=spot.open or 0,
                        low_price=spot.low or 0,
                        recent_closes=tech.get("recent_closes"),
                    )
                    for s in signals:
                        if s.score >= min_score and self._is_actionable_breakthrough(
                            s,
                            change_pct=spot.change_pct or 0,
                            min5_change=_spot_effective_min5_change(spot),
                            volume_ratio=_numeric_or_default(spot.volume_ratio, 1.0),
                        ):
                            events.append(AnomalyEvent(
                                code=code, name=spot.name,
                                event_type="breakthrough", level="major", score=s.score,
                                detail={
                                    **s.detail,
                                    "quality_score": float(getattr(s, "quality_score", 0) or 0),
                                    "ma_status": getattr(s, "ma_status", "") or "",
                                    "pullback_probability": getattr(s, "pullback_probability", "") or "",
                                    "days_near_pressure": int(getattr(s, "days_near_pressure", 0) or 0),
                                    "is_false_breakout": bool(getattr(s, "is_false_breakout", False)),
                                    **self._build_quote_detail(spot),
                                    **self._build_orderbook_detail(spot),
                                    **_derive_kline_boundary(tech, spot.price or 0),
                                    **sector_context,
                                    **board_context,
                                    "breakout_anchor": _safe_float(
                                        s.detail.get("high")
                                        or s.detail.get("value")
                                        or s.detail.get("boll_upper")
                                    ),
                                },
                                description=s.description,
                                is_ipo_recent=is_ipo,
                            ))

                positive_acceleration = self._detect_positive_acceleration_setup(
                    spot=spot,
                    current_fund=current_fund,
                    sector_context=sector_context,
                    board_context=board_context,
                    reversal_state=reversal_state,
                    today=today,
                    fund_snapshot_is_stale=fund_snapshot_is_stale,
                    fund_source=fund_source,
                    tech=tech,
                )
                if positive_acceleration:
                    acceleration_score, acceleration_label, acceleration_detail = (
                        positive_acceleration
                    )
                    if acceleration_score >= min_score:
                        events.append(AnomalyEvent(
                            code=code,
                            name=spot.name,
                            event_type="breakthrough",
                            level="major" if acceleration_score >= 78 else "minor",
                            score=acceleration_score,
                            detail=acceleration_detail,
                            description=acceleration_label,
                            is_ipo_recent=is_ipo,
                        ))

            # --- 强修复启动: 同产业链涨停簇 + 超跌低位 + 盘口确认 ---
            repair_driver = self._resolve_sector_repair_driver(sector_context)
            repair_prescreen = bool(
                lu is None
                and not is_ipo
                and repair_driver
                and str(code).startswith(("00", "60"))
                and 2.0 <= _safe_float(spot.change_pct) <= 6.2
                and 0.85 <= _safe_float(spot.volume_ratio, 1.0) <= 2.8
                and _safe_float(spot.amount) >= 1e8
            )
            if repair_prescreen:
                if tech is None:
                    tech = await self._calc_technical_indicators(code, session, target_date=today)
                repair_setup = self._detect_sector_repair_setup(
                    spot=spot,
                    tech=tech or {},
                    sector_context=sector_context,
                    board_context=board_context,
                    market_repair_context=market_repair_context,
                    today=today,
                )
                if repair_setup:
                    repair_score, repair_label, repair_detail = repair_setup
                    if repair_score >= min_score:
                        events.append(AnomalyEvent(
                            code=code,
                            name=spot.name,
                            event_type="breakthrough",
                            level="major",
                            score=repair_score,
                            detail=repair_detail,
                            description=repair_label,
                            is_ipo_recent=is_ipo,
                        ))

            # --- 旧高标超跌修复: 历史连板记忆 + 深跌沉淀 + 首日盘口/板块确认 ---
            old_hot_driver = self._resolve_old_hot_repair_driver(sector_context)
            old_hot_prescreen = bool(
                lu is None
                and not is_ipo
                and old_hot_driver
                and str(code).startswith(("00", "60"))
                and 3.5 <= _safe_float(spot.change_pct) <= 9.3
                and 0.8 <= _safe_float(spot.volume_ratio, 1.0) <= 2.6
                and _safe_float(spot.amount) >= 1.5e8
            )
            if old_hot_prescreen:
                if tech is None:
                    tech = await self._calc_technical_indicators(code, session, target_date=today)
                old_hot_setup = self._detect_old_hot_oversold_repair_setup(
                    spot=spot,
                    tech=tech or {},
                    sector_context=sector_context,
                    board_context=board_context,
                    today=today,
                )
                if old_hot_setup:
                    old_hot_score, old_hot_label, old_hot_detail = old_hot_setup
                    if old_hot_score >= min_score:
                        events.append(AnomalyEvent(
                            code=code,
                            name=spot.name,
                            event_type="breakthrough",
                            level="major",
                            score=old_hot_score,
                            detail=old_hot_detail,
                            description=old_hot_label,
                            is_ipo_recent=is_ipo,
                        ))

            # --- 动态趋势驱动池: 日K先入池，盘中只在支撑回收/放量突破时触发 ---
            if code in self.dynamic_trend_pool and lu is None:
                trend_setup = self._detect_trend_driver_watch_setup(
                    spot=spot,
                    current_fund=current_fund,
                    sector_context=sector_context,
                    board_context=board_context,
                    today=today,
                    reversal_state=reversal_state,
                    leader_spot=spot_by_code.get(
                        str(
                            (self.dynamic_trend_pool.get(code) or {})
                            .get("stats", {})
                            .get("leader_code")
                            or ""
                        )
                    ),
                )
                if trend_setup:
                    trend_event_type, trend_score, trend_label, trend_detail = trend_setup
                    if trend_score >= min_score:
                        events[:] = [
                            item
                            for item in events
                            if not (item.code == code and item.event_type == trend_event_type)
                        ]
                        events.append(AnomalyEvent(
                            code=code,
                            name=spot.name,
                            event_type=trend_event_type,
                            level="major" if trend_score >= 82 else "minor",
                            score=trend_score,
                            detail=trend_detail,
                            description=trend_label,
                            is_ipo_recent=is_ipo,
                        ))

            # --- 核心成长观察池: 只补充基本面与形态双确认信号 ---
            if code in self.low_base_watchlist_codes and lu is None:
                tech = await self._calc_technical_indicators(code, session, target_date=today)
                watch_setup = self._detect_low_base_watch_setup(
                    spot=spot,
                    tech=tech,
                    current_fund=current_fund,
                    sector_context=sector_context,
                    board_context=board_context,
                    today=today,
                    fund_source=fund_source,
                )
                if watch_setup:
                    watch_event_type, watch_score, watch_label, watch_detail = watch_setup
                    if watch_score >= min_score:
                        events[:] = [
                            item
                            for item in events
                            if not (item.code == code and item.event_type == watch_event_type)
                        ]
                        events.append(AnomalyEvent(
                            code=code,
                            name=spot.name,
                            event_type=watch_event_type,
                            level="major" if watch_score >= 82 else "minor",
                            score=watch_score,
                            detail=watch_detail,
                            description=watch_label,
                            is_ipo_recent=is_ipo,
                        ))

        # 水下反转池只保留本轮最强候选，页面仍展示代表性机会，飞书再走全局额度与冷却。
        underwater_events = [
            event for event in events
            if str(event.detail.get("signal_type") or "")
            in {"underwater_acceleration", "underwater_reversal"}
        ]
        if len(underwater_events) > 20:
            kept_underwater_ids = {
                id(event)
                for event in sorted(underwater_events, key=lambda item: item.score, reverse=True)[:20]
            }
            events = [
                event
                for event in events
                if str(event.detail.get("signal_type") or "")
                not in {"underwater_acceleration", "underwater_reversal"}
                or id(event) in kept_underwater_ids
            ]

        # 单次扫描只保留评分最高的修复买点，避免强修复日同质化概念映射造成
        # 页面和飞书候选泛滥；下一轮会按最新盘口重新竞争名额。
        repair_events = [
            event for event in events
            if str(event.detail.get("signal_type") or "") == "sector_repair_reversal"
        ]
        if len(repair_events) > 12:
            kept_repair_ids = {
                id(event)
                for event in sorted(repair_events, key=lambda item: item.score, reverse=True)[:12]
            }
            events = [
                event
                for event in events
                if str(event.detail.get("signal_type") or "") != "sector_repair_reversal"
                or id(event) in kept_repair_ids
            ]

        old_hot_events = [
            event for event in events
            if str(event.detail.get("signal_type") or "") == "old_hot_oversold_repair"
        ]
        if len(old_hot_events) > 8:
            kept_old_hot_ids = {
                id(event)
                for event in sorted(old_hot_events, key=lambda item: item.score, reverse=True)[:8]
            }
            events = [
                event
                for event in events
                if str(event.detail.get("signal_type") or "") != "old_hot_oversold_repair"
                or id(event) in kept_old_hot_ids
            ]

        for event in events:
            # Keep execution provenance separate from quote clocks and display data.
            event.detail["fund_signal_evidence"] = freeze_anomaly_main_fund(
                {**(current_fund_map.get(event.code) or {}),
                 "is_stale": bool(fund_snapshot_is_stale)
                 or bool((current_fund_map.get(event.code) or {}).get("is_stale"))},
                code=event.code, trade_date=today, decision_at=decision_at,
            )
            # Additive display evidence, frozen from this scan's actual fund frame.
            # Do not merge into detector detail: that would change entry/push gates.
            event.detail["fund_evidence"] = main_fund_display_evidence(
                current_fund_map.get(event.code) or {},
                trade_date=today, as_of_at=decision_at,
                basis="event_snapshot", event_at=decision_at,
            )
            if self._is_priority_watchlist_code(event.code):
                event.detail.setdefault("watchlist_member", True)
            if event.code in self.dynamic_trend_pool:
                setup = self.dynamic_trend_pool[event.code]
                setup_source = str(setup.get("source") or "")
                event.detail.setdefault(
                    "watchlist_label",
                    "动态低位连板观察池"
                    if setup_source == "pre_board_long_base"
                    else "中期动量缩量洗盘池"
                    if setup_source == "pre_board_momentum_shakeout"
                    else "动态试盘洗盘观察池"
                    if setup_source == "pre_board_probe_wash"
                    else "动态试盘突破观察池"
                    if setup_source == "pre_board_probe_breakout"
                    else "动态强修复观察池"
                    if setup_source in {"sector_repair_reversal", "old_hot_oversold_repair"}
                    else "重大事件接力观察池"
                    if setup_source in {"event_first_board", "event_high_board", "second_board_relay"}
                    else "看A做B联动池"
                    if setup_source == "leader_linkage_follow"
                    else "主线核心补涨池"
                    if setup_source == "sector_core_laggard"
                    else "牛股潜质回踩池"
                    if setup_source == "tenbagger_pullback_watch"
                    else "低位预期趋势确认池"
                    if setup_source == "low_expectation_trend_watch"
                    else "动态趋势驱动池",
                )
                event.detail.setdefault("trend_driver_watchlist", True)
                event.detail.setdefault(
                    "pre_board_watchlist",
                    setup_source.startswith("pre_board_"),
                )
                event.detail.setdefault("trend_setup_score", setup.get("score", 0))
            if event.code in self.low_base_watchlist_codes:
                event.detail.setdefault("watchlist_label", "核心成长观察池")
                event.detail.setdefault("core_growth_watchlist", True)
                event.detail.setdefault(
                    "core_growth_logic",
                    CORE_GROWTH_LOGIC_BY_CODE.get(event.code, "主营业务成长待持续验证"),
                )

        # 按评分降序
        events.sort(key=lambda e: e.score, reverse=True)
        return events

    # =========================================================================
    # 龙头追踪: 全市场板块龙头
    # =========================================================================

    async def scan_dragons(
        self,
        session: AsyncSession,
        target_date: date | None = None,
    ) -> list[DragonHeadResult]:
        """扫描全市场板块龙头"""
        # 获取今日有涨停的板块
        today = target_date or date.today()
        result = await session.execute(
            select(LimitUpPool.code, LimitUpPool.limit_up_reason).where(
                LimitUpPool.trade_date == today,
            )
        )
        lu_reason_by_code = {
            str(code): str(limit_up_reason or "").strip()
            for code, limit_up_reason in result.all()
            if code
        }
        lu_codes = list(lu_reason_by_code)

        if not lu_codes:
            return []

        # 获取这些股所在的板块
        result = await session.execute(
            select(
                StockSectorMapping.code,
                StockSectorMapping.sector_code,
                StockSectorMapping.sector_name,
                StockSectorMapping.sector_type,
                StockSectorMapping.source,
            )
            .where(StockSectorMapping.code.in_(lu_codes))
            .distinct()
        )
        raw_sector_rows = [
            {
                "seed_code": str(r[0] or ""),
                "sector_code": r[1],
                "sector_name": r[2],
                "sector_type": r[3],
                "source": r[4],
            }
            for r in result.all()
            if r[1]
        ]
        excluded_sector_codes: set[str] = set()
        sector_name_map: dict[str, str] = {}
        if raw_sector_rows:
            sector_info_result = await session.execute(
                select(SectorInfo.sector_code, SectorInfo.sector_name, SectorInfo.is_excluded).where(
                    SectorInfo.sector_code.in_([row["sector_code"] for row in raw_sector_rows]),
                )
            )
            for sector_code, sector_name, is_excluded in sector_info_result.all():
                if is_excluded == 1:
                    excluded_sector_codes.add(sector_code)
                if sector_name:
                    sector_name_map[sector_code] = sector_name

        sector_candidates_by_code: dict[str, dict] = {}
        for row in raw_sector_rows:
            sector_code = row["sector_code"]
            if sector_code in excluded_sector_codes:
                continue
            sector_name = str(row.get("sector_name") or sector_name_map.get(sector_code) or "").strip()
            sector_type = str(row.get("sector_type") or "")
            # 行业是个股唯一/主归属，可以直接承接涨停；概念是一对多映射，必须由
            # 当日涨停归因明确命中，避免“只因同时属于某宽泛概念”制造伪主线与伪龙头。
            if sector_type == "concept":
                seed_reason = lu_reason_by_code.get(str(row.get("seed_code") or ""), "")
                if self.dragon_scanner._theme_alignment_score(
                    {"limit_up_reason": seed_reason},
                    sector_name,
                ) < 75:
                    continue
            sector_candidates_by_code.setdefault(sector_code, {
                "sector_code": sector_code,
                "sector_name": sector_name,
                "sector_type": sector_type,
                "source": row.get("source") or "",
            })
        sector_candidates = list(sector_candidates_by_code.values())

        # 龙头追踪展示优先使用 pywencai/已有中文名板块。申万映射若只剩数字代码，
        # 仅在没有任何可展示板块名时兜底保留，避免页面“板块”列出现 730103 一类代码。
        named_candidates = [
            row for row in sector_candidates
            if _has_display_sector_name(row.get("sector_name"))
        ]
        display_candidates = named_candidates or sector_candidates
        display_candidates.sort(
            key=lambda row: (
                0 if row.get("source") == "pywencai" else 1,
                0 if row.get("sector_type") in {"concept", "industry"} else 1,
                str(row.get("sector_name") or row.get("sector_code") or ""),
            )
        )
        sectors = [
            (row["sector_code"], row.get("sector_name") or row["sector_code"])
            for row in display_candidates
        ]
        sector_meta_by_code = {
            str(row["sector_code"]): row
            for row in display_candidates
        }
        persistence_result = await session.execute(
            select(SectorPersistence).where(
                SectorPersistence.trade_date == today,
                SectorPersistence.sector_code.in_([sector_code for sector_code, _ in sectors]),
            )
        )
        persistence_by_code = {
            str(item.sector_code): item
            for item in persistence_result.scalars().all()
        }

        catalyst_map = await load_direct_stock_catalyst_map(
            session,
            today,
            candidate_codes=set(lu_codes),
            limit=max(600, len(lu_codes) * 20),
        )

        stocks_by_sector = await self._assemble_many_sector_stocks(
            session,
            [sector_code for sector_code, _ in sectors],
            target_date=today,
            catalyst_map=catalyst_map,
        )

        all_dragons = []
        for sector_code, sector_name in sectors:
            await asyncio.sleep(0)
            stocks = stocks_by_sector.get(sector_code) or []
            sector_meta = sector_meta_by_code.get(str(sector_code)) or {}
            if str(sector_meta.get("sector_type") or "") == "concept":
                stocks = [
                    stock
                    for stock in stocks
                    if not stock.get("is_limit_up")
                    or self.dragon_scanner._theme_alignment_score(stock, sector_name) >= 75
                ]
            if stocks:
                results = self.dragon_scanner.scan_sector(
                    sector_code,
                    sector_name,
                    stocks,
                    allow_linkage=_is_tradeable_linkage_sector(
                        sector_meta,
                        persistence_by_code.get(str(sector_code)),
                    ),
                )
                all_dragons.extend([
                    result
                    for result in results
                    if result.level in ("dragon", "quasi_dragon")
                    and result.link_role in {"leader_a", "follower_b"}
                ])

        all_dragons.sort(
            key=lambda result: (
                1 if result.link_role == "leader_a" else 0,
                result.recognition_score if result.link_role == "leader_a" else result.linkage_score,
                result.tradability_score,
            ),
            reverse=True,
        )
        return all_dragons

    # =========================================================================
    # 个股综合评分
    # =========================================================================

    async def score_stock(
        self,
        code: str,
        session: AsyncSession,
        target_date: date | None = None,
    ) -> BullScoreResult:
        """个股综合评分 — 完整6维度"""
        decision_at = datetime.now()
        trade_day = target_date or date.today()
        spot_result = await session.execute(
            select(StockSpot).where(StockSpot.code == code)
        )
        spot = spot_result.scalar_one_or_none()

        name = spot.name if spot else code

        # 1. 龙头评分
        dragon_result = None
        if spot:
            result = await session.execute(
                select(StockSectorMapping.sector_code, StockSectorMapping.sector_name)
                .where(StockSectorMapping.code == code)
                .limit(1)
            )
            sector_info = result.first()
            if sector_info:
                stocks = await self._assemble_sector_stocks(session, sector_info[0], target_date=trade_day)
                if stocks:
                    dragon_results = self.dragon_scanner.scan_sector(
                        sector_info[0], sector_info[1], stocks
                    )
                    dragon_result = next(
                        (r for r in dragon_results if r.code == code), None
                    )

        # 2. 连板评分(P1补全)
        promotion_result = await self._get_promotion_result(code, session, target_date=trade_day)

        # 3. 资金评分
        capital_anomalies = []
        if spot:
            qualified = await load_current_main_fund_map(
                session, trade_date=trade_day, decision_at=decision_at, codes=[code],
            )
            current_fund_data = self._current_fund_evidence(
                qualified.get(code), trade_day, decision_at,
            )

            # The single-stock path obeys the same five completed sessions as
            # the market scan. Old latest-five rows across gaps are not a window.
            fund_window = await load_main_fund_window(
                session, through_date=trade_day, decision_at=decision_at, codes=[code],
            )
            fund_history = fund_window["items"].get(code, {})
            recent_funds = (
                list(reversed(fund_history["history"])) if fund_history.get("complete") else []
            )

            if not current_fund_data.get("is_stale", True):
                capital_anomalies = self.capital_detector.detect(
                    code=code, name=name,
                    fund_data=current_fund_data,
                    recent_funds=recent_funds,
                    volume_ratio=_numeric_or_default(spot.volume_ratio, 1.0),
                    change_pct=spot.change_pct or 0,
                    open_price=spot.open or 0,
                    high_price=spot.high or 0,
                    low_price=spot.low or 0,
                    close_price=spot.price,
                )

        # 4. 筹码评分
        chip_result = None
        result = await session.execute(
            select(StockKline.turnover, StockKline.change_pct, StockKline.volume,
                   StockKline.close, StockKline.high, StockKline.low)
            .where(
                StockKline.code == code,
                StockKline.trade_date <= trade_day,
            )
            .order_by(desc(StockKline.trade_date))
            .limit(30)
        )
        klines_data = result.all()
        if len(klines_data) >= 5:
            chip_result = self.chip_analyzer.analyze(
                code=code, name=name,
                recent_turnovers=[k[0] for k in klines_data if k[0]],
                recent_changes=[k[1] for k in klines_data if k[1]],
                recent_volumes=[k[2] for k in klines_data if k[2]],
                recent_closes=[k[3] for k in klines_data if k and len(k) > 3 and k[3]],
                recent_highs=[k[4] for k in klines_data if k and len(k) > 4 and k[4]],
                recent_lows=[k[5] for k in klines_data if k and len(k) > 5 and k[5]],
                current_price=spot.price if spot else 0,
            )

        # 5. 突破评分
        breakthrough_signals = []
        if spot:
            tech = await self._calc_technical_indicators(code, session, target_date=trade_day)
            if tech:
                breakthrough_signals = self.breakthrough_detector.detect(
                    code=code, name=name,
                    price=spot.price,
                    volume=_spot_volume_in_kline_unit(spot.volume),
                    high_20d=tech.get("high_20d", 0),
                    high_60d=tech.get("high_60d", 0),
                    high_120d=tech.get("high_120d", 0),
                    ma5=tech.get("ma5", 0),
                    ma10=tech.get("ma10", 0),
                    ma20=tech.get("ma20", 0),
                    ma60=tech.get("ma60", 0),
                    boll_upper=tech.get("boll_upper", 0),
                    avg_volume_20d=tech.get("avg_vol_20d", 0),
                    open_price=spot.open or 0,
                    low_price=spot.low or 0,
                    recent_closes=tech.get("recent_closes"),
                )

        # 6. 情绪评分(从MarketSentiment获取)
        sentiment_score = 50
        sentiment_cycle = "recovery"

        # 综合评分
        chip_signal_val = chip_result.signal_strength if chip_result else 0.0
        return self.bull_model.score(
            code=code, name=name,
            dragon_result=dragon_result,
            promotion_result=promotion_result,
            capital_anomalies=capital_anomalies,
            chip_result=chip_result,
            breakthrough_signals=breakthrough_signals,
            sentiment_score=sentiment_score,
            sentiment_cycle=sentiment_cycle,
            chip_signal=chip_signal_val,
        )

    # =========================================================================
    # 共振分析
    # =========================================================================

    async def analyze_resonance(
        self,
        code: str,
        session: AsyncSession,
        target_date: date | None = None,
        *, as_of_at: datetime | None = None,
    ) -> dict:
        """个股-板块共振分析

        3维度: 方向一致(40) + 资金共振(30) + 生命周期(30) = 100
        四级: 强共振(80+)/弱共振(60+)/独立(40+)/逆势(<40)
        """
        fund_decision_at = as_of_at or datetime.now()
        trade_day = target_date or fund_decision_at.date()
        # 1. 个股数据。观察池个股可能没有实时 spot，用最新日K做只读共振兜底。
        spot_result = await session.execute(
            select(StockSpot).where(StockSpot.code == code)
        )
        spot = spot_result.scalar_one_or_none()
        if not spot:
            kline_result = await session.execute(
                select(StockKline)
                .where(
                    StockKline.code == code,
                    StockKline.trade_date <= trade_day,
                )
                .order_by(desc(StockKline.trade_date))
                .limit(1)
            )
            latest_kline = kline_result.scalar_one_or_none()
            if not latest_kline:
                return {"code": code, "error": "无行情数据"}

            tag_result = await session.execute(
                select(StockTag).where(StockTag.code == code).limit(1)
            )
            tag = tag_result.scalar_one_or_none()
            spot = SimpleNamespace(
                code=code,
                name=(tag.name if tag and tag.name else code),
                change_pct=latest_kline.change_pct or 0,
                price=latest_kline.close or 0,
            )

        # 2. 所属板块
        result = await session.execute(
            select(StockSectorMapping.sector_code, StockSectorMapping.sector_name)
            .where(StockSectorMapping.code == code)
            .limit(5)
        )
        sectors = result.all()

        if not sectors:
            return {
                "code": code, "name": spot.name,
                "stock_change": spot.change_pct,
                "resonance_score": 0,
                "level": "independent",
                "purpose": "research_only",
                "fund_contract_version": "strict_current_dated_display_v1",
                "trade_date": trade_day.isoformat(),
                "main_net_inflow": None,
                "main_fund_status": "not_evaluated",
                "main_fund_display": None,
                "sectors": [],
            }

        fund_status: dict[str, str] = {}
        current_funds = await load_current_main_fund_map(
            session, trade_date=trade_day, decision_at=fund_decision_at,
            codes=[code], diagnostics=fund_status,
        )
        current_main_inflow = (current_funds.get(code) or {}).get("main_net_inflow")
        fund_display = await load_latest_main_fund_display_map(
            session, trade_date=trade_day, as_of_at=fund_decision_at, codes=[code],
        )
        # SectorPersistence has dated values, not a verified live source clock.
        # The combined score is research-only, never executable fund evidence.
        # 3. 逐板块计算共振
        sector_results = []
        best_score = 0
        best_level = "independent"

        for sector_code, sector_name in sectors:
            # 板块行情(从SectorPersistence)
            sp_result = await session.execute(
                select(SectorPersistence)
                .where(
                    SectorPersistence.sector_code == sector_code,
                    SectorPersistence.trade_date <= trade_day,
                )
                .order_by(desc(SectorPersistence.trade_date))
                .limit(1)
            )
            sp = sp_result.scalar_one_or_none()

            sector_change = sp.change_pct if sp else 0
            sector_fund = (
                sp.fund_flow if sp and sp.trade_date == trade_day
                and main_fund_values_valid(sp.fund_flow, 0) else None
            )
            consecutive_days = sp.consecutive_days if sp else 0

            # 方向一致分(0-40)
            stock_up = (spot.change_pct or 0) > 0
            sector_up = sector_change > 0
            if stock_up and sector_up:
                direction_score = 40 if sector_change > 2 else 30
            elif not stock_up and not sector_up:
                direction_score = 25  # 同跌, 弱共振
            elif stock_up and not sector_up:
                # P2: 独立行情增强判定
                if spot.change_pct > 5:
                    direction_score = 25  # 逆势大涨=独立强势
                else:
                    direction_score = 5   # 弱独立
            else:
                direction_score = 10  # 跟跌不跟涨

            # Missing/stale evidence is not a measured zero (the zero branch is 5).
            # Preserve all original scores for qualified numeric inputs.
            if current_main_inflow is None or sector_fund is None:
                fund_score = 0
            elif current_main_inflow > 0 and sector_fund > 0:
                fund_score = 30
            elif current_main_inflow > 0 and sector_fund <= 0:
                fund_score = 10
            elif current_main_inflow < 0 and sector_fund < 0:
                fund_score = 15  # 同流出
            else:
                fund_score = 5

            # 生命周期分(0-30)
            if consecutive_days >= 5:
                lifecycle_score = 30  # 持续活跃
            elif consecutive_days >= 3:
                lifecycle_score = 20
            elif consecutive_days >= 1:
                lifecycle_score = 10
            else:
                lifecycle_score = 5

            total = direction_score + fund_score + lifecycle_score

            # 判定等级
            if total >= 80:
                level = "strong_resonance"
            elif total >= 60:
                level = "weak_resonance"
            elif total >= 40:
                level = "independent"
            else:
                level = "counter_trend"

            # P2: 逆势大涨独立标注
            independent_strength = ""
            if stock_up and not sector_up and (spot.change_pct or 0) > 5:
                independent_strength = "strong_independent"

            sector_results.append({
                "sector_code": sector_code,
                "sector_name": sector_name,
                "sector_change": round(sector_change, 2),
                "sector_fund": round(sector_fund, 2) if sector_fund is not None else None,
                "sector_fund_trade_date": sp.trade_date.isoformat() if sp else None,
                "sector_fund_status": "dated_unclocked" if sector_fund is not None else "unknown",
                "direction_score": direction_score,
                "fund_score": fund_score,
                "lifecycle_score": lifecycle_score,
                "total_score": total,
                "level": level,
                "independent_strength": independent_strength,
            })

            if total > best_score:
                best_score = total
                best_level = level

        return {
            "code": code,
            "name": spot.name,
            "stock_change": spot.change_pct,
            "best_resonance_score": best_score,
            "best_level": best_level,
            "purpose": "research_only",
            "fund_contract_version": "strict_current_dated_display_v1",
            "trade_date": trade_day.isoformat(),
            "main_net_inflow": current_main_inflow,
            "main_fund_status": fund_status.get(code, "missing"),
            "main_fund_decision_at": fund_decision_at.isoformat(),
            "main_fund_display": fund_display.get(code),
            "sectors": sector_results,
        }


# 全局单例
anomaly_scanner = AnomalyScanner()
