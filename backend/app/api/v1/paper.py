"""模拟盘 API"""

import asyncio
import json
import logging
import math
import re
import uuid
from contextvars import ContextVar
from copy import deepcopy
from types import SimpleNamespace
from datetime import date, datetime, time, timedelta
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, BeforeValidator, Field, field_validator, model_validator
from sqlalchemy import and_, desc, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import OperationalError

from app.config.settings import settings
from app.paper.account_policy import account_confirmation_policy
from app.paper.confirmation_evidence import (
    confirmation_evidence, freeze_log_evidence, historical_confirmation_evidence,
    order_confirmation_evidence, deferred_buy_log_evidence,
)
from app.paper.experiment import execution_version, standard_execution_version, experiment_active, experiment_order_override, experiment_status, sentiment_is_required, sentiment_quality_at
from app.core.price_limit_rules import is_limit_up_change, price_limit_rule
from app.core.stock_tagger import BOARD_TAG_MAP, CODE_PREFIX_MAP, TAG_TRADEABLE, stock_tagger
from app.core.trade_calendar import trade_calendar
from app.db.session import get_db
from app.models.paper import (
    PaperAccount,
    PaperAutoTradeLog,
    PaperControlSample,
    PaperDailyOutcome,
    PaperNav,
    PaperPosition,
    PaperTradeLog,
)
from app.models.stock import LimitUpPool, MarketSentiment, SectorPersistence, StockBlacklist, StockKline, StockSectorMapping, StockSpot, StockTag
from app.risk.circuit_breaker import sentiment_circuit_breaker
from app.risk.engine import RiskContext, risk_engine
from app.trading.service import validate_order_notional, validated_order_price
from app.promotion.versioning import ProbabilityContractError, resolve_promotion_probability

router = APIRouter()
logger = logging.getLogger(__name__)
_TRADE_LOCK = asyncio.Lock()
_AUTO_TRADE_LOCKS: dict[str, asyncio.Lock] = {}
_ACCOUNT_INIT_LOCK = asyncio.Lock()
# Broker-local Challenger metadata only, NOT execution authorization.
# Public HTTP always rejects these accounts; paper_authorization guards booking.
_CHALLENGER_INTERNAL_ORDER_CONTEXT: ContextVar[bool] = ContextVar(
    "paper_challenger_internal_order",
    default=False,
)
_AUTO_STRATEGY_VERSION_CONTEXT: ContextVar[str] = ContextVar(
    "paper_auto_strategy_version",
    default="",
)
# 由调度器按任务上下文注入，不可变 owned dict；同一账户整轮都读取同一快照。
_QUOTE_ROUND_CONTEXT: ContextVar[dict] = ContextVar("paper_quote_round", default={})
# broker 在真正撮合时注入成交轮次/时钟，manual API 保持空上下文。
_PAPER_FILL_CONTEXT: ContextVar[dict] = ContextVar("paper_fill_context", default={})


def _account_auto_lock(account_name: str) -> asyncio.Lock:
    return _AUTO_TRADE_LOCKS.setdefault(str(account_name or PAPER_ACCOUNT_DEFAULT), asyncio.Lock())


def _paper_now() -> datetime:
    for payload in (_PAPER_FILL_CONTEXT.get(), _QUOTE_ROUND_CONTEXT.get()):
        value = payload.get("committed_at") if isinstance(payload, dict) else None
        if isinstance(value, datetime):
            return value
    return datetime.now()

_PAPER_ACTIONABLE_PLAN_TYPES = {
    # 与 tenbagger._NEXT_DAY_ACTIONABLE_STRATEGIES 保持一致, 避免牛股雷达
    # 明日预案显示可执行而模拟盘不买的不一致 (2026-08-31 评审修复).
    "strong_get_stronger",
    "main_wave_confirm",
    "trend",
    "trend_pullback_buy",
    "trend_breakout_buy",
    "repair_followup_buy",
    "aggressive",
}
_PAPER_GENERIC_SECTOR_KEYWORDS = (
    "融资融券",
    "转融券",
    "沪股通",
    "深股通",
    "富时罗素",
    "MSCI",
    "标普道琼斯",
    "证金持股",
)
_PAPER_BUY_SOURCE_LABELS = {
    "next_day_plan": "高胜率预案",
    "green_limit_reversal": "绿盘转强",
    "underwater_reversal": "水下翻红",
    "anomaly_buy_point": "到达买点",
    "ma5_pullback": "5日线回踩",
    "daily_participation": "每日参与",
    "icepoint_reversal": "冰点转强",
    "promotion_promotion": "晋级二板",
    "promotion_mainline": "主线扩散首板",
    "promotion_auction": "竞价高开强攻",
    "tenbagger_midline": "连板高标接力",
    "reversal_pullback": "断板反包",
    "b_weak_open_second_board": "B2负开弱转强",
    "c_recent_limit_relaunch": "C2涨停记忆再启动",
    "d_auction_recovery": "D2竞价恢复",
    "f2_highboard_break_reclaim": "F2高标断板收复",
    "position-t": "日内做T",
    "position": "持仓风控",
    "system": "系统",
}


class SimBuyRequest(BaseModel):
    code: str
    price: Annotated[float, BeforeValidator(validated_order_price)] = Field(gt=0)
    amount: int = Field(ge=100, multiple_of=100)
    signal_id: str = ""
    reason: str = ""
    stop_loss_price: Optional[float] = None
    entry_sector_code: Optional[str] = None
    entry_sector_name: Optional[str] = None

    @field_validator("stop_loss_price", mode="before")
    @classmethod
    def finite_optional_stop(cls, value):
        if value is None:
            return None
        number = _to_float(value)
        if number is None:
            raise ValueError("止损价不能为非有限数或布尔值")
        return number

    @model_validator(mode="after")
    def finite_order_amount(self):
        validate_order_notional(self.price, self.amount)
        return self


class SimSellRequest(BaseModel):
    code: str
    price: Annotated[float, BeforeValidator(validated_order_price)] = Field(gt=0)
    amount: int = Field(ge=100, multiple_of=100)
    signal_id: str = ""
    reason: str = ""

    @model_validator(mode="after")
    def finite_order_amount(self):
        validate_order_notional(self.price, self.amount)
        return self


class AutoRunRequest(BaseModel):
    execute: bool = True
    trigger: str = "manual"
    max_candidates: int = Field(default=20, ge=1, le=100)
    execution_mode: str = Field(default="intraday", pattern="^(manual|intraday|close)$")


def _parse_hhmm(value: str, fallback: time) -> time:
    try:
        hour, minute = str(value).split(":", 1)
        return time(int(hour), int(minute))
    except Exception:
        return fallback


def _is_intraday_buy_window(
    now: Optional[datetime] = None,
    *,
    start_value: Optional[str] = None,
) -> bool:
    now = now or datetime.now()
    current = now.time()
    start = _parse_hhmm(
        start_value or settings.PAPER_INTRADAY_BUY_START,
        time(9, 35),
    )
    end = _parse_hhmm(settings.PAPER_INTRADAY_BUY_END, time(14, 50))
    return start <= current <= end and trade_calendar.get_trade_session(now) in {"morning", "afternoon"}


def _is_recovery_buy_time_window(now: Optional[datetime] = None) -> bool:
    now = now or datetime.now()
    current = now.time()
    start = _parse_hhmm(settings.PAPER_INTRADAY_BUY_START, time(9, 35))
    end = _parse_hhmm(settings.PAPER_AUTO_DRAWDOWN_RECOVERY_BUY_END, time(14, 30))
    return start <= current <= end and trade_calendar.get_trade_session(now) in {"morning", "afternoon"}


def _is_daily_participation_time(now: Optional[datetime] = None) -> bool:
    now = now or datetime.now()
    current = now.time()
    start = _parse_hhmm(settings.PAPER_AUTO_DAILY_PARTICIPATION_START, time(10, 30))
    end = _parse_hhmm(settings.PAPER_AUTO_LATE_NEW_BUY_CUTOFF, time(14, 30))
    return start <= current <= end and trade_calendar.get_trade_session(now) in {"morning", "afternoon"}


def _is_icepoint_reversal_time(now: Optional[datetime] = None) -> bool:
    now = now or datetime.now()
    current = now.time()
    start = _parse_hhmm(settings.PAPER_AUTO_ICEPOINT_REVERSAL_START, time(10, 15))
    end = _parse_hhmm(settings.PAPER_AUTO_LATE_NEW_BUY_CUTOFF, time(14, 30))
    return start <= current <= end and trade_calendar.get_trade_session(now) in {"morning", "afternoon"}


def _is_afternoon_new_buy_time(now: Optional[datetime] = None) -> bool:
    now = now or datetime.now()
    start = _parse_hhmm(settings.PAPER_AUTO_AFTERNOON_NEW_BUY_START, time(13, 0))
    return now.time() >= start and trade_calendar.get_trade_session(now) == "afternoon"


def _is_late_new_buy_time(now: Optional[datetime] = None) -> bool:
    now = now or datetime.now()
    cutoff = _parse_hhmm(settings.PAPER_AUTO_LATE_NEW_BUY_CUTOFF, time(14, 30))
    return now.time() > cutoff


def _is_open_noise_window(now: Optional[datetime] = None, *, end_value: Optional[str] = None) -> bool:
    now = now or datetime.now()
    end = _parse_hhmm(end_value if end_value is not None else settings.PAPER_AUTO_OPEN_NOISE_END, time(9, 45))
    return time(9, 30) <= now.time() < end


async def _paper_order_window_status(now: Optional[datetime] = None) -> tuple[bool, str]:
    now = now or datetime.now()
    if not await trade_calendar.is_trade_day(now.date()):
        return False, "非交易日，模拟盘不下单"
    session = trade_calendar.get_trade_session(now)
    if session not in {"morning", "afternoon"}:
        return False, f"当前时段{session}，不在A股连续竞价成交窗口"
    return True, ""


async def _should_run_intraday_auto_trade(now: Optional[datetime] = None) -> tuple[bool, str]:
    now = now or datetime.now()
    if not settings.PAPER_INTRADAY_AUTO_TRADE_ENABLED:
        return False, "盘中自动交易开关未启用"
    if not await trade_calendar.is_trade_day(now.date()):
        return False, "非交易日，盘中自动交易跳过"
    session = trade_calendar.get_trade_session(now)
    if session not in {"morning", "afternoon"}:
        return False, f"当前时段{session}，不做盘中自动交易"
    return True, ""


# 策略账户常量: 五策略并行 (2026-08-31) + 策略F断板反包 (2026-08-31 晚)
PAPER_ACCOUNT_DEFAULT = "default"          # 策略A: 现有模拟盘链路(预案+盘中快照)
PAPER_ACCOUNT_PROMOTION = "promotion"      # 策略B: 晋级预测二板赛道(Challenger 实验策略)
PAPER_ACCOUNT_MAINLINE = "mainline"        # 策略C: 晋级预测·主线扩散首板（持续模拟）
PAPER_ACCOUNT_AUCTION = "auction"          # 策略D: 晋级预测·竞价高开强攻（持续模拟）
PAPER_ACCOUNT_TENBAGGER = "tenbagger"      # 策略E: 连板高标接力
PAPER_ACCOUNT_REVERSAL = "reversal"        # 策略F: 断板反包(连板≥3深跌断板后放量反包, 12/8/3)

# 六个隔离 paper 子账户：A2/B2/C2/D2/F2接收前向确认；E2独立高标扫描。
# 控制样本保留为排除绩效的审计账，不代表A2/E2成交或替代交易账户。
PAPER_ACCOUNT_CHALLENGER_A = "challenger_a"
PAPER_ACCOUNT_CHALLENGER_B = "challenger_b"
PAPER_ACCOUNT_CHALLENGER_C = "challenger_c"
PAPER_ACCOUNT_CHALLENGER_D = "challenger_d"
PAPER_ACCOUNT_CHALLENGER_E = "challenger_e"
PAPER_ACCOUNT_CHALLENGER_F2 = "challenger_f2"
PAPER_CHALLENGER_BASE_ACCOUNT = {
    PAPER_ACCOUNT_CHALLENGER_A: PAPER_ACCOUNT_DEFAULT,
    PAPER_ACCOUNT_CHALLENGER_B: PAPER_ACCOUNT_PROMOTION,
    PAPER_ACCOUNT_CHALLENGER_C: PAPER_ACCOUNT_MAINLINE,
    PAPER_ACCOUNT_CHALLENGER_D: PAPER_ACCOUNT_AUCTION,
    PAPER_ACCOUNT_CHALLENGER_E: PAPER_ACCOUNT_TENBAGGER,
    PAPER_ACCOUNT_CHALLENGER_F2: PAPER_ACCOUNT_REVERSAL,
}
PAPER_CONTROL_CHALLENGER_BY_BASE = {
    PAPER_ACCOUNT_DEFAULT: PAPER_ACCOUNT_CHALLENGER_A,
    PAPER_ACCOUNT_TENBAGGER: PAPER_ACCOUNT_CHALLENGER_E,
}
PAPER_CHALLENGER_ACCOUNT_BY_ROUTE = {
    "momentum_first_retest": PAPER_ACCOUNT_CHALLENGER_A,
    "b_weak_open_second_board": PAPER_ACCOUNT_CHALLENGER_B,
    "c_recent_limit_relaunch": PAPER_ACCOUNT_CHALLENGER_C,
    "d_auction_recovery": PAPER_ACCOUNT_CHALLENGER_D,
    "f2_highboard_break_reclaim": PAPER_ACCOUNT_CHALLENGER_F2,
}
PAPER_CHALLENGER_ACCOUNTS = tuple(PAPER_CHALLENGER_BASE_ACCOUNT)


def _base_strategy_account(account_name: str) -> str:
    return PAPER_CHALLENGER_BASE_ACCOUNT.get(account_name, account_name)


def _strategy_version(account_name: str) -> str:
    """执行协议变更独立版本；旧成交保留其入场版本，不混入新实验。"""
    base = _strategy_base_version(account_name)
    if settings.PAPER_CONTINUOUS_EXPERIMENT_ENABLED and account_name in (*PAPER_ALL_ACCOUNTS, *PAPER_CHALLENGER_ACCOUNTS):
        return execution_version(base, account_name=account_name)
    if account_name in (*PAPER_ALL_ACCOUNTS, *PAPER_CHALLENGER_ACCOUNTS):
        return standard_execution_version(base, account_name=account_name)
    return base


def _strategy_base_version(account_name: str) -> str:
    if account_name == PAPER_ACCOUNT_CHALLENGER_A:
        return f"{settings.PAPER_CHALLENGER_A_VERSION}:{settings.PAPER_MOMENTUM_RETEST_SHADOW_VERSION}"
    if account_name == PAPER_ACCOUNT_CHALLENGER_E:
        return settings.PAPER_CHALLENGER_E_VERSION
    if account_name in PAPER_CHALLENGER_ACCOUNTS:
        route = next(
            (
                route_id
                for route_id, challenger in PAPER_CHALLENGER_ACCOUNT_BY_ROUTE.items()
                if challenger == account_name
            ),
            account_name,
        )
        return f"{settings.PAPER_STRATEGY_ITERATION_SHADOW_VERSION}:{route}"
    return {
        PAPER_ACCOUNT_DEFAULT: settings.PAPER_STRATEGY_A_VERSION,
        PAPER_ACCOUNT_PROMOTION: settings.PAPER_STRATEGY_B_VERSION,
        PAPER_ACCOUNT_MAINLINE: settings.PAPER_STRATEGY_C_VERSION,
        PAPER_ACCOUNT_AUCTION: settings.PAPER_STRATEGY_D_VERSION,
        PAPER_ACCOUNT_TENBAGGER: settings.PAPER_STRATEGY_E_VERSION,
        PAPER_ACCOUNT_REVERSAL: settings.PAPER_STRATEGY_F_VERSION,
    }.get(account_name, f"paper_custom:{account_name}")


PAPER_OPEN_CONFIRM_CONTEXTS = ("promotion_0925", "promotion_0935")
PAPER_MAINLINE_INTRADAY_CONTEXTS = (
    "promotion_0935",
    "promotion_1000",
    "promotion_1030",
    "promotion_1305",
)
PAPER_MAINLINE_ALLOWED_CONTEXTS = (
    "promotion_0925",
    *PAPER_MAINLINE_INTRADAY_CONTEXTS,
)

# 调度器/前端共用的全账户顺序 (策略A在前, 实验策略在后)
PAPER_ALL_ACCOUNTS = (
    PAPER_ACCOUNT_DEFAULT,
    PAPER_ACCOUNT_PROMOTION,
    PAPER_ACCOUNT_MAINLINE,
    PAPER_ACCOUNT_AUCTION,
    PAPER_ACCOUNT_TENBAGGER,
    PAPER_ACCOUNT_REVERSAL,
)

PAPER_SCAN_ACCOUNTS = (*PAPER_ALL_ACCOUNTS, PAPER_ACCOUNT_CHALLENGER_E)

PAPER_PROMOTION_ACCOUNTS = {
    PAPER_ACCOUNT_PROMOTION: {
        "route": "second_board_promotion",
        "target_board": 2,
        "enabled": lambda: settings.PAPER_PROMOTION_ENABLED,
        "min_probability": lambda: settings.PAPER_PROMOTION_MIN_PROBABILITY,
        "enforce_probability_floor": True,
        "min_confirm_change": lambda: settings.PAPER_PROMOTION_MIN_INTRADAY_CONFIRM_CHANGE_PCT,
        "max_confirm_change": lambda: settings.PAPER_PROMOTION_MAX_INTRADAY_CONFIRM_CHANGE_PCT,
        "label": "晋级预测二板",
    },
    PAPER_ACCOUNT_MAINLINE: {
        "route": "mainline_spread_start",
        "target_board": 1,
        "enabled": lambda: settings.PAPER_MAINLINE_ENABLED,
        "min_probability": lambda: settings.PAPER_MAINLINE_MIN_PROBABILITY,
        # 首板概率是1%~10%的低基准率校准尺度，不能再套旧版40%绝对阈值。
        # 上游actionable优先；缺少主线专属主动确认时，仅走09:35同板块二次确认。
        "enforce_probability_floor": False,
        "min_confirm_change": lambda: settings.PAPER_MAINLINE_MIN_INTRADAY_CONFIRM_CHANGE_PCT,
        "max_confirm_change": lambda: settings.PAPER_MAINLINE_MAX_INTRADAY_CONFIRM_CHANGE_PCT,
        "label": "主线扩散首板",
    },
    PAPER_ACCOUNT_AUCTION: {
        "route": "auction_surge_start",
        "target_board": 1,
        "enabled": lambda: settings.PAPER_AUCTION_ENABLED,
        "min_probability": lambda: settings.PAPER_AUCTION_MIN_PROBABILITY,
        # D继续严格消费不可变快照的actionable契约，不重复套用旧概率尺度。
        "enforce_probability_floor": False,
        "min_confirm_change": lambda: settings.PAPER_AUCTION_MIN_INTRADAY_CONFIRM_CHANGE_PCT,
        "max_confirm_change": lambda: settings.PAPER_AUCTION_MAX_INTRADAY_CONFIRM_CHANGE_PCT,
        "label": "竞价高开强攻",
    },
}


def _strategy_intraday_buy_start(account_name: str) -> str:
    """高标主次账户各自配置起始时刻；其余策略保留原去噪窗口。"""
    if account_name == PAPER_ACCOUNT_CHALLENGER_E:
        return settings.PAPER_CHALLENGER_E_INTRADAY_BUY_START
    if _base_strategy_account(account_name) == PAPER_ACCOUNT_TENBAGGER:
        return settings.PAPER_HIGHBOARD_INTRADAY_BUY_START
    return settings.PAPER_INTRADAY_BUY_START


def _strategy_auto_order_enabled(account_name: str, *, now: Optional[datetime] = None) -> bool:
    """持续实验覆盖旧策略暂停；启动前只观察和管理退出，不补历史单。"""
    override = experiment_order_override(account_name, at=now or _paper_now())
    if override is not None:
        return override
    return {
        PAPER_ACCOUNT_CHALLENGER_A: settings.PAPER_CHALLENGER_A_AUTO_ORDER_ENABLED,
        PAPER_ACCOUNT_CHALLENGER_B: settings.PAPER_CHALLENGER_B_AUTO_ORDER_ENABLED,
        PAPER_ACCOUNT_CHALLENGER_C: settings.PAPER_CHALLENGER_C_AUTO_ORDER_ENABLED,
        PAPER_ACCOUNT_CHALLENGER_D: settings.PAPER_CHALLENGER_D_AUTO_ORDER_ENABLED,
        PAPER_ACCOUNT_CHALLENGER_E: settings.PAPER_CHALLENGER_E_AUTO_ORDER_ENABLED,
        PAPER_ACCOUNT_CHALLENGER_F2: settings.PAPER_CHALLENGER_F2_AUTO_ORDER_ENABLED,
        PAPER_ACCOUNT_PROMOTION: settings.PAPER_PROMOTION_AUTO_ORDER_ENABLED,
        PAPER_ACCOUNT_MAINLINE: settings.PAPER_MAINLINE_AUTO_ORDER_ENABLED,
        PAPER_ACCOUNT_AUCTION: settings.PAPER_AUCTION_AUTO_ORDER_ENABLED,
        PAPER_ACCOUNT_TENBAGGER: settings.PAPER_TENBAGGER_AUTO_ORDER_ENABLED,
        PAPER_ACCOUNT_REVERSAL: settings.PAPER_REVERSAL_AUTO_ORDER_ENABLED,
    }.get(account_name, True)


def _strategy_buy_limits(account_name: str) -> tuple[int, int]:
    """返回(单日最多新开仓数, 最大持仓数)，A2使用其冻结参数。"""
    if account_name in PAPER_CHALLENGER_ACCOUNTS:
        from app.paper.account_policy import account_entry_exit_policy
        policy = account_entry_exit_policy(account_name)
        return int(policy["max_daily_buys"]), int(policy["max_positions"])
    account_name = _base_strategy_account(account_name)
    return {
        PAPER_ACCOUNT_PROMOTION: (
            settings.PAPER_PROMOTION_MAX_DAILY_BUYS,
            settings.PAPER_PROMOTION_MAX_POSITIONS,
        ),
        PAPER_ACCOUNT_MAINLINE: (
            settings.PAPER_MAINLINE_MAX_DAILY_BUYS,
            settings.PAPER_MAINLINE_MAX_POSITIONS,
        ),
        PAPER_ACCOUNT_AUCTION: (
            settings.PAPER_AUCTION_MAX_DAILY_BUYS,
            settings.PAPER_AUCTION_MAX_POSITIONS,
        ),
        PAPER_ACCOUNT_TENBAGGER: (
            settings.PAPER_TENBAGGER_MAX_DAILY_BUYS,
            settings.PAPER_TENBAGGER_MAX_POSITIONS,
        ),
        PAPER_ACCOUNT_REVERSAL: (
            settings.PAPER_REVERSAL_MAX_DAILY_BUYS,
            settings.PAPER_REVERSAL_MAX_POSITIONS,
        ),
    }.get(
        account_name,
        (settings.PAPER_AUTO_MAX_DAILY_NEW_BUYS, settings.PAPER_AUTO_MAX_POSITIONS),
    )


# 各策略账户的实际卖出参数。评估端与执行端共用，避免C/D/E/F沿用A阈值。
def _strategy_sell_params_by_name(account_name: str) -> dict:
    """完整的逐账户退出策略；持仓执行时还要读取建仓冻结版本。"""
    if account_name not in (*PAPER_ALL_ACCOUNTS, *PAPER_CHALLENGER_ACCOUNTS):
        return {}
    from app.paper.account_policy import account_sell_params
    return account_sell_params(account_name)


def _strategy_empty_reason(account_name: str) -> str:
    """候选为空时, 按账户策略返回对应的说明文案 (2026-08-31 五策略评审修复)."""
    return {
        PAPER_ACCOUNT_PROMOTION: "晋级预测二板赛道暂无满足概率阈值的候选，不启用旧候选池兜底",
        PAPER_ACCOUNT_MAINLINE: "主线扩散首板赛道暂无上游可执行候选，也无当日09:35榜单完成同板块实时扩散确认，不启用旧候选池兜底",
        PAPER_ACCOUNT_AUCTION: "竞价高开强攻赛道暂无竞价字段完整且治理快照已判定可执行的候选，不启用旧候选池兜底",
        PAPER_ACCOUNT_TENBAGGER: "连板高标接力暂无满足连板数/封板质量的候选，不启用旧候选池兜底",
        PAPER_ACCOUNT_REVERSAL: "断板反包暂无满足连板/深跌/放量条件的候选，不启用旧候选池兜底",
    }.get(
        account_name,
        "高胜率明日预案和异动买点暂无满足自动买入条件的样本，不启用旧候选池兜底",
    )


async def _get_or_create_account(
    db: AsyncSession,
    account_name: str = PAPER_ACCOUNT_DEFAULT,
) -> PaperAccount:
    """按账户名获取或创建模拟盘账户 (多策略并行)."""
    account_name = account_name or PAPER_ACCOUNT_DEFAULT
    lookup = (
        select(PaperAccount)
        .where(PaperAccount.account_name == account_name, PaperAccount.status == "active")
        .order_by(PaperAccount.id)
        .limit(1)
    )
    # Existing accounts need no initialization lock. A SELECT may autoflush:
    # holding the Python lock while waiting for SQLite's writer can deadlock
    # another writer which re-enters this lookup before committing.
    result = await db.execute(lookup)
    account = result.scalar_one_or_none()
    if account:
        return account

    # Only first creation is serialized; recheck after acquiring the lock so
    # simultaneous first readers cannot create duplicate active accounts/NAVs.
    async with _ACCOUNT_INIT_LOCK:
        result = await db.execute(lookup)
        account = result.scalar_one_or_none()
        if account:
            return account

        # 兼容旧库: 历史数据只在 default 账户, 新账户默认 5 万初始资金
        initial_capital = settings.PAPER_INITIAL_CAPITAL
        account = PaperAccount(
            account_name=account_name,
            strategy=_base_strategy_account(account_name) if (
                account_name in PAPER_ALL_ACCOUNTS or account_name in PAPER_CHALLENGER_ACCOUNTS
            ) else "default",
            initial_capital=initial_capital,
            current_capital=initial_capital,
            total_assets=initial_capital,
            total_return=0,
            max_drawdown=0,
            sharpe_ratio=0,
            win_rate=0,
            status="active",
        )
        db.add(account)
        await db.flush()
        now = _paper_now()
        if await _nav_reporting_day_open(db, now):
            db.add(PaperNav(account_id=account.id, trade_date=now.date(), nav=1, daily_return=None))
        from app.trading.paper_authorization import finish_account_write
        await finish_account_write(db)
        await db.refresh(account)
        return account


async def _stock_info(db: AsyncSession, code: str) -> tuple[Optional[str], Optional[float]]:
    spot = await _spot_by_code(db, code)
    if spot:
        return spot.name, spot.price
    tag = (await db.execute(select(StockTag).where(StockTag.code == code))).scalar_one_or_none()
    return (tag.name if tag else None), None


def _commission(amount_value: float) -> float:
    calculated = max(float(amount_value or 0), 0.0) * settings.PAPER_COMMISSION_RATE
    minimum = max(float(getattr(settings, "PAPER_MIN_COMMISSION", 0) or 0), 0.0)
    return round(max(calculated, minimum) if amount_value > 0 else 0.0, 2)


def _stamp_tax(amount_value: float) -> float:
    return round(amount_value * settings.PAPER_STAMP_TAX_RATE, 2)


async def _existing_paper_trade(
    db: AsyncSession,
    *,
    account_id: int,
    trade_type: str,
    signal_id: str,
) -> PaperTradeLog | None:
    """按账户/方向/成交请求键回放，封住撮合提交后的重试窗口。"""
    normalized = str(signal_id or "").strip()
    if not normalized:
        return None
    return await db.scalar(
        select(PaperTradeLog)
        .where(
            PaperTradeLog.account_id == account_id,
            PaperTradeLog.trade_type == trade_type,
            PaperTradeLog.signal_id == normalized,
        )
        .order_by(desc(PaperTradeLog.id))
        .limit(1)
    )


async def _refresh_expired_paper_rows(db: AsyncSession, *rows):
    """Rollback expires caller objects too; reload persisted leaves before logging."""
    from sqlalchemy import inspect
    for row in rows:
        state = inspect(row, raiseerr=False)
        if state is not None and state.persistent and state.expired:
            await db.refresh(row)


async def _open_positions(db: AsyncSession, account_id: int) -> list[PaperPosition]:
    from app.trading.paper_authorization import paper_transaction_active
    result = await db.execute(
        select(PaperPosition)
        .where(PaperPosition.account_id == account_id, PaperPosition.is_closed.is_(False))
        .order_by(PaperPosition.buy_time)
        # Pre-risk may have cached inventory before another fill acquired the lock.
        .execution_options(populate_existing=paper_transaction_active(db))
    )
    return result.scalars().all()


async def _cash_from_trades(db: AsyncSession, account: PaperAccount) -> float:
    result = await db.execute(
        select(PaperTradeLog).where(PaperTradeLog.account_id == account.id)
    )
    cash = float(account.initial_capital or 0)
    for trade in result.scalars().all():
        trade_value = float(trade.price or 0) * int(trade.amount or 0)
        fee = float(trade.commission or 0) + float(getattr(trade, "tax", 0) or 0)
        if trade.trade_type == "buy":
            cash -= trade_value + fee
        elif trade.trade_type == "sell":
            cash += trade_value - fee
    return round(cash, 2)


async def _nav_reporting_calendar(db: AsyncSession, start: date, end: date):
    """Read-only calendar view: reuse exchange overrides and local calendar facts.

    Never invoke the global calendar's network sync from a reporting helper.
    Missing local dates use the existing calendar's weekday fallback.
    """
    from app.core.trade_calendar import TradeCalendar
    from app.models.governance import TradeCalendarModel

    calendar = TradeCalendar()
    day = start
    while day <= end:
        calendar._cache[day] = trade_calendar._cache.get(day, day.weekday() < 5)
        day += timedelta(days=1)
    with db.no_autoflush:
        rows = (await db.scalars(select(TradeCalendarModel).where(
            TradeCalendarModel.trade_date >= start,
            TradeCalendarModel.trade_date <= end,
        ))).all()
    for row in rows:
        calendar._cache[row.trade_date] = bool(row.is_trade_day)
    return calendar


async def _nav_reporting_day_open(db: AsyncSession, now: datetime) -> bool:
    from app.core.trade_calendar import TRADE_SESSIONS

    calendar = await _nav_reporting_calendar(db, now.date(), now.date())
    return (
        await calendar.is_trade_day(now.date())
        and now.time() >= TRADE_SESSIONS["morning"][0]
    )


async def _filter_reporting_nav(
    db: AsyncSession, rows: list[PaperNav], *, now: datetime | None = None,
) -> list[PaperNav]:
    """Filter display only; retain historical rows and risk inputs unchanged."""
    now = now or _paper_now()
    eligible = [
        row for row in rows
        if isinstance(row.trade_date, date) and row.trade_date <= now.date()
        and isinstance(row.nav, (int, float)) and math.isfinite(row.nav) and row.nav > 0
    ]
    if not eligible:
        return []
    calendar = await _nav_reporting_calendar(
        db, min(row.trade_date for row in eligible), now.date(),
    )
    from app.core.trade_calendar import TRADE_SESSIONS

    return [
        row for row in eligible
        if await calendar.is_trade_day(row.trade_date)
        and (row.trade_date < now.date() or now.time() >= TRADE_SESSIONS["morning"][0])
    ]


async def _refresh_account(db: AsyncSession, account: PaperAccount) -> PaperAccount:
    positions = await _open_positions(db, account.id)
    market_value = 0.0
    valuation_now = _paper_now()
    today = valuation_now.date()
    for position in positions:
        name, spot_price = await _stock_info(db, position.code)
        current_price = spot_price or position.current_price or position.buy_price
        position.name = position.name or name
        position.current_price = current_price
        position.profit_loss = round((current_price - position.buy_price) * position.buy_amount, 2)
        position.profit_pct = round((current_price / position.buy_price - 1) * 100, 2) if position.buy_price else 0
        position.hold_days = await _trade_day_hold_days(position.buy_time.date(), today)
        market_value += current_price * position.buy_amount

    account.current_capital = await _cash_from_trades(db, account)
    account.total_assets = round((account.current_capital or 0) + market_value, 2)
    account.total_return = round((account.total_assets / account.initial_capital - 1) * 100, 2) if account.initial_capital else 0

    nav_value = round(account.total_assets / account.initial_capital, 6) if account.initial_capital else 1
    # Do not invent weekend/pre-open marks or backfill the previous session.
    if await _nav_reporting_day_open(db, valuation_now):
        calendar = await _nav_reporting_calendar(db, today - timedelta(days=40), today)
        previous_date = await calendar.previous_trade_day(today)
        previous_nav = await db.scalar(select(PaperNav).where(
            PaperNav.account_id == account.id,
            PaperNav.trade_date == previous_date,
        ))
        daily_return = None
        if (
            previous_nav is not None
            and previous_nav.nav is not None
            and math.isfinite(previous_nav.nav) and previous_nav.nav > 0
            and account.initial_capital is not None
            and math.isfinite(account.initial_capital) and account.initial_capital > 0
            and math.isfinite(account.total_assets) and account.total_assets >= 0
        ):
            previous_assets = account.initial_capital * previous_nav.nav
            if math.isfinite(previous_assets) and previous_assets > 0:
                value = (account.total_assets / previous_assets - 1) * 100
                if math.isfinite(value):
                    daily_return = round(value, 2)
        await db.execute(
            text(
                """
                INSERT INTO paper_nav (account_id, trade_date, nav, daily_return)
                VALUES (:account_id, :trade_date, :nav, :daily_return)
                ON CONFLICT(account_id, trade_date)
                DO UPDATE SET nav = excluded.nav, daily_return = excluded.daily_return
                """
            ),
            {
                "account_id": account.id,
                "trade_date": today.isoformat(),
                "nav": nav_value,
                "daily_return": daily_return,
            },
        )

    nav_values = (
        await db.execute(
            select(PaperNav.nav)
            .where(PaperNav.account_id == account.id)
            .order_by(PaperNav.trade_date)
        )
    ).scalars().all()
    nav_values = [float(value or 0) for value in nav_values if value is not None]
    account.max_drawdown = _calc_drawdown(nav_values)
    account.current_drawdown = _calc_current_drawdown(nav_values)
    trade_rows = (
        await db.execute(
            select(PaperTradeLog)
            .where(PaperTradeLog.account_id == account.id)
            .order_by(PaperTradeLog.trade_time, PaperTradeLog.id)
        )
    ).scalars().all()
    closed_trades = [
        float(trade.realized_pnl)
        for trade in trade_rows
        if trade.trade_type == "sell" and trade.realized_pnl is not None
    ]
    if closed_trades:
        wins = sum(1 for pnl in closed_trades if pnl is not None and pnl > 0)
        account.win_rate = round(wins / len(closed_trades), 4)
    else:
        account.win_rate = 0
    trade_stats = _profit_stats_from_pnls(closed_trades)
    round_trip_samples = await _round_trip_samples(trade_rows)
    round_trip_stats = _profit_stats_from_pnls([float(item["pnl"] or 0) for item in round_trip_samples])
    account.trade_stats = trade_stats
    account.round_trip_stats = round_trip_stats
    account.round_trip_win_rate = round_trip_stats["win_rate"]
    account.fee_drag = round(sum(float(trade.commission or 0) + float(getattr(trade, "tax", 0) or 0) for trade in trade_rows), 2)
    account.trade_count = len(trade_rows)
    account.turnover = round(sum(float(trade.price or 0) * int(trade.amount or 0) for trade in trade_rows), 2)
    from app.trading.paper_authorization import finish_account_write
    await finish_account_write(db)
    await db.refresh(account)
    account.current_drawdown = _calc_current_drawdown(nav_values)
    account.trade_stats = trade_stats
    account.round_trip_stats = round_trip_stats
    account.round_trip_win_rate = round_trip_stats["win_rate"]
    account.fee_drag = round(sum(float(trade.commission or 0) + float(getattr(trade, "tax", 0) or 0) for trade in trade_rows), 2)
    account.trade_count = len(trade_rows)
    account.turnover = round(sum(float(trade.price or 0) * int(trade.amount or 0) for trade in trade_rows), 2)
    return account


def _strategy_display_meta(account: PaperAccount) -> dict:
    """返回策略的展示元数据 (标签/一句话说明/短名), 供前端做辨识度区分.

    2026-08-31: 五策略账户数字相同(空账户都是5万), 前后端需要能一眼区分当前策略.
    """
    acc = str(getattr(account, "account_name", "") or "")
    meta = {
        PAPER_ACCOUNT_DEFAULT: {
            "label": "策略A · 高胜率预案",
            "desc": "明日预案+盘中快照，短线混合，止盈5.5%/止损5%",
            "short": "A",
        },
        PAPER_ACCOUNT_PROMOTION: {
            "label": "策略B · 晋级二板",
            "desc": "晋级预测二板持续模拟实验，止盈{}/止损{}/持仓3天".format(
                settings.PAPER_PROMOTION_TAKE_PROFIT_PCT, settings.PAPER_PROMOTION_STOP_LOSS_PCT
            ),
            "short": "B",
        },
        PAPER_ACCOUNT_MAINLINE: {
            "label": "策略C · 主线扩散首板",
            "desc": "主线扩散首板持续模拟实验；止盈{}/止损{}/持仓3天".format(
                settings.PAPER_MAINLINE_TAKE_PROFIT_PCT, settings.PAPER_MAINLINE_STOP_LOSS_PCT
            ),
            "short": "C",
        },
        PAPER_ACCOUNT_AUCTION: {
            "label": "策略D · 竞价高开强攻",
            "desc": "完整竞价证据与高开确认持续模拟实验；止盈5%/止损4%/持仓2天",
            "short": "D",
        },
        PAPER_ACCOUNT_TENBAGGER: {
            "label": "策略E · 连板高标接力",
            "desc": "连板{}-{}高标接力，止盈{}/止损{}/持仓{}日".format(
                settings.PAPER_HIGHBOARD_MIN_CONSECUTIVE,
                settings.PAPER_HIGHBOARD_MAX_CONSECUTIVE,
                settings.PAPER_HIGHBOARD_TAKE_PROFIT_PCT,
                settings.PAPER_HIGHBOARD_STOP_LOSS_PCT,
                settings.PAPER_HIGHBOARD_MAX_HOLD_DAYS,
            ),
            "short": "E",
        },
        PAPER_ACCOUNT_REVERSAL: {
            "label": "策略F · 断板反包",
            "desc": "连板≥{}深跌断板后放量反包持续模拟，止盈{}/止损{}/持仓{}日".format(
                settings.PAPER_REVERSAL_MIN_CONSECUTIVE,
                settings.PAPER_REVERSAL_TAKE_PROFIT_PCT,
                settings.PAPER_REVERSAL_STOP_LOSS_PCT,
                settings.PAPER_REVERSAL_MAX_HOLD_DAYS,
            ),
            "short": "F",
        },
    }
    challenger_meta = {
        PAPER_ACCOUNT_CHALLENGER_A: {
            "label": "策略A2 · 首次回踩确认（持续模拟）",
            "desc": "独立消费首次回踩确认，10%预算；止盈8%/止损5%/持仓5日；下一轮真实盘口撮合",
            "short": "A2",
        },
        PAPER_ACCOUNT_CHALLENGER_B: {
            "label": "策略B2 · 负开弱转强（隔离候选）",
            "desc": "隔离模拟子账户：昨日首板，今日弱开后收复零轴与日内成交均价线",
            "short": "B2",
        },
        PAPER_ACCOUNT_CHALLENGER_C: {
            "label": "策略C2 · 涨停记忆再启动（隔离候选）",
            "desc": "隔离模拟子账户：近期低板记忆，短暂调整后盘中再收复",
            "short": "C2",
        },
        PAPER_ACCOUNT_CHALLENGER_D: {
            "label": "策略D2 · 竞价恢复（隔离候选）",
            "desc": "隔离模拟子账户：仅消费完整早期与最终竞价配对后的弱转强",
            "short": "D2",
        },
        PAPER_ACCOUNT_CHALLENGER_E: {
            "label": "策略E2 · 高标强势/回封（持续模拟）",
            "desc": "与E共享昨日封单/连板质量和退出约束，入口扩展至强势/回封；封死无卖盘不伪造成交",
            "short": "E2",
        },
        PAPER_ACCOUNT_CHALLENGER_F2: {
            "label": "策略F2 · 高标断板收复（隔离候选）",
            "desc": "隔离模拟子账户：高标断板1至3日后，从水下或零轴收复",
            "short": "F2",
        },
    }
    if acc in challenger_meta:
        return challenger_meta[acc]
    return meta.get(acc, {
        "label": f"策略{acc}",
        "desc": "自定义策略",
        "short": acc[:1].upper(),
    })


async def _accounting_display(db: AsyncSession, account: PaperAccount) -> dict:
    # Reporting only: do not attach these values to ORM/risk state.
    from app.paper.accounting import load_accounting
    from app.paper.strategy_iteration_challenger import _account_return_breakdown

    now = _paper_now()
    result = await load_accounting(db, account, as_of=now.date())
    daily = await _account_return_breakdown(db, account, now=now)
    result["today_mtm"] = {
        key: daily[key] for key in (
            "daily_pnl", "daily_return_pct", "daily_trade_date",
            "daily_status", "daily_note",
        )
    }
    return result


def _account_payload(account: PaperAccount) -> dict:
    strategy_meta = _strategy_display_meta(account)
    return {
        "id": account.id,
        "account_name": account.account_name,
        "strategy": getattr(account, "strategy", None) or _base_strategy_account(account.account_name),
        "strategy_version": _strategy_version(account.account_name),
        "experiment": experiment_status(account.account_name, at=_paper_now()),
        "auto_buy_enabled": _strategy_auto_order_enabled(account.account_name),
        "is_challenger": account.account_name in PAPER_CHALLENGER_ACCOUNTS,
        "champion_account_name": PAPER_CHALLENGER_BASE_ACCOUNT.get(account.account_name),
        "broker": "paper",
        "execution_plane": "simulation",
        "broker_sync_verified": False,
        "real_order_connected": False,
        "real_order_guard": "hard_blocked",
        "account_scope": "active_only",
        "archived_legacy_count": int(getattr(account, "archived_legacy_count", 0) or 0),
        "strategy_label": strategy_meta["label"],
        "strategy_desc": strategy_meta["desc"],
        "strategy_short": strategy_meta["short"],
        "initial_capital": account.initial_capital,
        "current_capital": account.current_capital,
        "cash": account.current_capital,
        "total_assets": account.total_assets,
        "total_return": account.total_return,
        "max_drawdown": account.max_drawdown,
        "current_drawdown": getattr(account, "current_drawdown", account.max_drawdown),
        "sharpe_ratio": account.sharpe_ratio,
        "win_rate": account.win_rate,
        "trade_win_rate": account.win_rate,
        "round_trip_win_rate": getattr(account, "round_trip_win_rate", None),
        "trade_stats": getattr(account, "trade_stats", {}),
        "round_trip_stats": getattr(account, "round_trip_stats", {}),
        "fee_drag": getattr(account, "fee_drag", 0),
        "trade_count": getattr(account, "trade_count", 0),
        "turnover": getattr(account, "turnover", 0),
        "status": account.status,
    }


def _position_payload(position: PaperPosition) -> dict:
    return {
        "id": position.id,
        "code": position.code,
        "name": position.name,
        "buy_price": position.buy_price,
        "buy_amount": position.buy_amount,
        "amount": position.buy_amount,
        "buy_time": position.buy_time.isoformat(sep=" "),
        "buy_reason": position.buy_reason,
        "strategy_version": getattr(position, "strategy_version", None),
        "entry_sector_code": getattr(position, "entry_sector_code", None),
        "entry_sector_name": getattr(position, "entry_sector_name", None),
        "current_price": position.current_price,
        "profit_loss": position.profit_loss,
        "profit_pct": position.profit_pct,
        "hold_days": position.hold_days,
        "stop_loss_price": position.stop_loss_price,
        "is_closed": position.is_closed,
    }


def _trade_payload(trade: PaperTradeLog) -> dict:
    return {
        "id": trade.id,
        "code": trade.code,
        "trade_type": trade.trade_type,
        "type": trade.trade_type,
        "price": trade.price,
        "amount": trade.amount,
        "shares": trade.amount,
        "trade_time": trade.trade_time.isoformat(sep=" "),
        "commission": trade.commission,
        "tax": getattr(trade, "tax", 0),
        "total_fee": round(float(trade.commission or 0) + float(getattr(trade, "tax", 0) or 0), 2),
        "signal_id": trade.signal_id,
        "reason": trade.reason,
        "pnl": trade.realized_pnl,
        "realized_pnl": trade.realized_pnl,
        "strategy_version": getattr(trade, "strategy_version", None),
        "decision_round_id": getattr(trade, "decision_round_id", None),
        "fill_round_id": getattr(trade, "fill_round_id", None),
        "forced_probe": bool(getattr(trade, "forced_probe", False)),
        "excluded_from_performance": bool(getattr(trade, "excluded_from_performance", False)),
    }


def _calc_drawdown(values: list[float]) -> float:
    if not values:
        return 0.0
    peak = values[0]
    max_drawdown = 0.0
    for value in values:
        peak = max(peak, value)
        if peak:
            max_drawdown = min(max_drawdown, (value / peak - 1) * 100)
    return round(max_drawdown, 2)


def _calc_current_drawdown(values: list[float]) -> float:
    if not values:
        return 0.0
    peak = max(values)
    latest = values[-1]
    return round((latest / peak - 1) * 100, 2) if peak else 0.0


async def _round_trip_samples(trades: list[PaperTradeLog]) -> list[dict]:
    """按代码聚合从首次买入到仓位归零的完整交易轮次。"""
    state: dict[str, dict] = {}
    samples: list[dict] = []
    for trade in sorted(trades, key=lambda item: (item.trade_time, item.id or 0)):
        code = trade.code
        bucket = state.setdefault(code, {
            "code": code,
            "amount": 0,
            "buy_cash": 0.0,
            "sell_cash": 0.0,
            "pnl": 0.0,
            "buy_time": None,
            "sell_time": None,
            "buy_count": 0,
            "sell_count": 0,
        })
        amount = int(trade.amount or 0)
        commission = float(trade.commission or 0) + float(getattr(trade, "tax", 0) or 0)
        gross = float(trade.price or 0) * amount
        if trade.trade_type == "buy":
            if bucket["amount"] <= 0:
                bucket.update({
                    "amount": 0,
                    "buy_cash": 0.0,
                    "sell_cash": 0.0,
                    "pnl": 0.0,
                    "buy_time": trade.trade_time,
                    "sell_time": None,
                    "buy_count": 0,
                    "sell_count": 0,
                })
            bucket["amount"] += amount
            bucket["buy_cash"] += gross + commission
            bucket["buy_count"] += 1
        elif trade.trade_type == "sell":
            bucket["amount"] -= amount
            bucket["sell_cash"] += gross - commission
            bucket["pnl"] += float(trade.realized_pnl or 0)
            bucket["sell_time"] = trade.trade_time
            bucket["sell_count"] += 1
            if bucket["amount"] <= 0 and bucket["buy_count"] > 0:
                base = float(bucket["buy_cash"] or 0)
                pnl = round(float(bucket["pnl"] or 0), 2)
                hold_days = (
                    await _trade_day_hold_days(
                        bucket["buy_time"].date(),
                        bucket["sell_time"].date(),
                    )
                    if bucket["buy_time"] and bucket["sell_time"] else None
                )
                samples.append({
                    "code": code,
                    "buy_time": bucket["buy_time"].isoformat(sep=" ") if bucket["buy_time"] else None,
                    "sell_time": bucket["sell_time"].isoformat(sep=" ") if bucket["sell_time"] else None,
                    "buy_count": bucket["buy_count"],
                    "sell_count": bucket["sell_count"],
                    "pnl": pnl,
                    "return_pct": round(pnl / base * 100, 2) if base else None,
                    "hold_days": hold_days,
                })
                state[code] = {
                    "code": code,
                    "amount": 0,
                    "buy_cash": 0.0,
                    "sell_cash": 0.0,
                    "pnl": 0.0,
                    "buy_time": None,
                    "sell_time": None,
                    "buy_count": 0,
                    "sell_count": 0,
                }
    return samples


def _profit_stats_from_pnls(pnls: list[float]) -> dict:
    wins = [pnl for pnl in pnls if pnl > 0]
    losses = [pnl for pnl in pnls if pnl < 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    return {
        "count": len(pnls),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(pnls), 4) if pnls else None,
        "avg_pnl": round(sum(pnls) / len(pnls), 2) if pnls else None,
        "total_pnl": round(sum(pnls), 2),
        "avg_win": round(gross_profit / len(wins), 2) if wins else None,
        "avg_loss": round(sum(losses) / len(losses), 2) if losses else None,
        "profit_factor": round(gross_profit / gross_loss, 4) if gross_loss else (None if not gross_profit else 999.0),
        "payoff_ratio": round((gross_profit / len(wins)) / abs(sum(losses) / len(losses)), 4) if wins and losses else None,
        "expectancy": round(sum(pnls) / len(pnls), 2) if pnls else None,
    }


def _empty_eval_bucket(label: str) -> dict:
    return {
        "label": label,
        "count": 0,
        "wins": 0,
        "losses": 0,
        "win_rate": None,
        "avg_pnl": None,
        "total_pnl": 0.0,
        "avg_win": None,
        "avg_loss": None,
        "profit_factor": None,
        "payoff_ratio": None,
        "expectancy": None,
        "avg_return_pct": None,
        "max_drawdown": 0.0,
        "avg_hold_days": None,
    }


def _finalize_eval_bucket(bucket: dict, samples: list[dict]) -> dict:
    count = len(samples)
    if not count:
        return bucket
    pnls = [float(item.get("pnl") or 0) for item in samples]
    returns = [float(item.get("return_pct") or 0) for item in samples if item.get("return_pct") is not None]
    hold_days = [float(item.get("hold_days") or 0) for item in samples if item.get("hold_days") is not None]
    pnl_stats = _profit_stats_from_pnls(pnls)
    equity = []
    running = 0.0
    for pnl in pnls:
        running += pnl
        equity.append(running)
    bucket.update({
        "count": count,
        "wins": pnl_stats["wins"],
        "losses": pnl_stats["losses"],
        "win_rate": pnl_stats["win_rate"],
        "avg_pnl": pnl_stats["avg_pnl"],
        "total_pnl": pnl_stats["total_pnl"],
        "avg_win": pnl_stats["avg_win"],
        "avg_loss": pnl_stats["avg_loss"],
        "profit_factor": pnl_stats["profit_factor"],
        "payoff_ratio": pnl_stats["payoff_ratio"],
        "expectancy": pnl_stats["expectancy"],
        "avg_return_pct": round(sum(returns) / len(returns), 2) if returns else None,
        "max_drawdown": _calc_drawdown(equity),
        "avg_hold_days": round(sum(hold_days) / len(hold_days), 2) if hold_days else None,
    })
    return bucket


def _json_dumps(value) -> str:
    return json.dumps(value or {}, ensure_ascii=False, default=str)


def _json_loads_dict(value) -> dict:
    try:
        parsed = json.loads(value or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _to_float(value) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _number_or(value, fallback: float) -> float:
    parsed = _to_float(value)
    return fallback if parsed is None else parsed


def _quote_round_context() -> dict:
    payload = _QUOTE_ROUND_CONTEXT.get()
    return payload if isinstance(payload, dict) else {}


def _round_spot(code: str):
    payload = _quote_round_context()
    records_by_code = payload.get("records_by_code")
    if records_by_code is None and payload.get("records"):
        records_by_code = {
            str(item.get("code") or ""): item
            for item in payload["records"]
            if isinstance(item, dict) and item.get("code")
        }
        payload["records_by_code"] = records_by_code
    record = (records_by_code or {}).get(str(code or ""))
    return SimpleNamespace(**record) if isinstance(record, dict) else None


def _paper_main_fund_cutoff(trade_date: date, decision_at: datetime | None = None) -> datetime | None:
    cutoff = decision_at or _paper_now()
    if not isinstance(cutoff, datetime) or cutoff.tzinfo is not None or cutoff.date() != trade_date:
        return None
    context = _quote_round_context()
    if context.get("round_id"):
        as_of = context.get("as_of_at")
        if not isinstance(as_of, datetime) or as_of.tzinfo is not None or as_of.date() != trade_date:
            return None
        cutoff = min(cutoff, as_of)
    return cutoff


async def _paper_main_fund_map(
    db: AsyncSession, *, trade_date: date, codes=None, decision_at: datetime | None = None,
) -> dict:
    """只读当时可见真实资金；不得把后来取得的资金嫁接到旧报价轮次。"""
    from app.data.main_fund import load_current_main_fund_map

    cutoff = _paper_main_fund_cutoff(trade_date, decision_at)
    if cutoff is None:
        return {}
    return await load_current_main_fund_map(
        db, trade_date=trade_date, decision_at=cutoff, codes=codes,
    )


def _main_fund_evidence(item: dict | None) -> dict:
    """独立资金证据，不声称资金来自腾讯QuoteRound，不覆盖原行情记录。"""
    if not item:
        return {"main_net_inflow": None, "main_net_inflow_pct": None,
                "main_fund_evidence": {"status": "unknown",
                    "reason": "真实主力资金unknown：缺失/过期/时钟或来源不合格",
                    "dataset": "fund_flow"}}
    evidence = {"status": "known", "dataset": "fund_flow"}
    for key in ("source", "source_version", "source_quote_at", "received_at", "observed_at", "date",
                "main_fund_policy", "source_clock_basis"):
        value = item.get(key)
        evidence[key] = value.isoformat() if isinstance(value, (date, datetime)) else value
    return {"main_net_inflow": item["main_net_inflow"],
            "main_net_inflow_pct": item["main_net_inflow_pct"],
            "main_fund_evidence": evidence}


def _bind_main_fund_evidence(candidate: dict, fund: dict | None) -> None:
    """浅拷贝外部detail中的资金叶字段，防止顶层修正后仍携带较晚/旧资金证据。"""
    evidence = _main_fund_evidence(fund)
    current = fund or {}
    fund_fields = (
        "main_net_inflow", "main_net_inflow_pct",
        "super_net_inflow", "super_net_inflow_pct",
        "big_net_inflow", "big_net_inflow_pct",
        "mid_net_inflow", "mid_net_inflow_pct",
        "small_net_inflow", "small_net_inflow_pct",
        "main_pct", "super_pct", "big_pct", "mid_pct", "small_pct",
    )
    # 合格API未返回的细分必须unknown，不能继承旧快照并随主力额标为新鲜。
    candidate.update({key: current.get(key) for key in fund_fields if key in candidate})
    candidate.update(evidence)
    detail = candidate.get("detail")
    if isinstance(detail, dict) and any(key in detail for key in fund_fields):
        candidate["detail"] = {
            **detail, **{key: current.get(key) for key in fund_fields}, **evidence,
            "source": "fund_flow" if fund else "unavailable",
            "provider_source": current.get("source"),
            "source_version": current.get("source_version"),
            "source_quote_at": current.get("source_quote_at"),
            "received_at": current.get("received_at"),
            "observed_at": current.get("observed_at"),
            "clock_status": "ok" if fund else "unknown",
            "is_stale": fund is None,
        }


async def _confirm_candidate_main_fund(db, candidate, spot, *, trade_date: date, decision_at: datetime) -> str:
    """执行前重验原资金分支；盘口替代条件保留，不给E/F增加资金门。"""
    source = str(candidate.get("_source") or "")
    if source not in {"green_limit_reversal", "underwater_reversal", "ma5_pullback",
                      "daily_participation", "icepoint_reversal", "next_day_plan", "anomaly_buy_point"}:
        return ""
    code = str(candidate.get("code") or "")
    funds = await _paper_main_fund_map(db, trade_date=trade_date, codes=[code], decision_at=decision_at)
    _bind_main_fund_evidence(candidate, funds.get(code))
    flow = candidate["main_net_inflow"]
    positive = flow is not None and flow > 0
    missing = candidate["main_fund_evidence"]["status"] == "unknown"
    reason = ("真实主力资金unknown：缺失/过期/不可见，不能用腾讯委差或旧候选资金确认"
              if missing else "真实主力净流入未大于0")
    if source in {"green_limit_reversal", "underwater_reversal"}:
        return "" if positive else reason
    value = lambda key: _to_float(getattr(spot, key, None))
    support, book, bid = value("support_strength_score"), value("orderbook_imbalance"), value("bid_ratio")
    if source == "ma5_pullback":
        if not (positive or (support is not None and support >= 55) or (book is not None and book >= .08)):
            return reason + "，盘口承接亦未确认"
    elif source == "icepoint_reversal":
        if not (positive or (bid is not None and bid > 0)):
            return reason + "，委比亦未为正"
    elif source == "daily_participation":
        pct = candidate["main_net_inflow_pct"]
        pct_ok = pct is not None and pct >= settings.PAPER_AUTO_DAILY_PARTICIPATION_MIN_MAIN_INFLOW_PCT
        liquidity = sum((support is not None and support >= settings.PAPER_AUTO_DAILY_PARTICIPATION_MIN_SUPPORT_STRENGTH,
                         book is not None and book >= settings.PAPER_AUTO_DAILY_PARTICIPATION_MIN_ORDERBOOK, pct_ok))
        confirmations = [text for text in candidate.get("intraday_confirmations", [])
                         if text not in {"主力净流入占比达标", "增量买盘为正"}]
        if pct_ok:
            confirmations.append("主力净流入占比达标")
        if positive or (bid is not None and bid > 0):
            confirmations.append("增量买盘为正")
        candidate["intraday_confirmations"] = confirmations
        candidate["liquidity_confirmation_count"] = liquidity
        if (liquidity < settings.PAPER_AUTO_DAILY_PARTICIPATION_MIN_LIQUIDITY_CONFIRMATIONS
                or len(confirmations) < settings.PAPER_AUTO_DAILY_PARTICIPATION_MIN_CONFIRMATIONS):
            return reason + "，每日参与原资金/盘口确认数不足"
    return ""


async def _funded_reversal_rows(db, rows, *, trade_date: date, limit: int, underwater: bool):
    funds = await _paper_main_fund_map(db, trade_date=trade_date, codes=[row.code for row in rows])
    unknown = [str(row.code) for row in rows if row.code not in funds]
    selected = [row for row in rows if row.code in funds and funds[row.code]["main_net_inflow"] > 0]
    def rank(row):
        flow = funds[row.code]["main_net_inflow"]
        change = _number_or(getattr(row, "change_pct", None), float("-inf"))
        return (flow, change) if underwater else (change, flow)
    selected.sort(key=rank, reverse=True)
    notes = ([f"真实主力资金unknown（缺失/过期/不可见）：{','.join(unknown)}"] if unknown else [])
    return selected[:max(limit * (16 if underwater else 12), 120 if underwater else 80)], funds, notes


async def _spot_by_code(db: AsyncSession, code: str):
    payload = _quote_round_context()
    if payload.get("round_id"):
        # 行情轮次上下文中缺一只即失败关闭，不能悄悄读取更新后的 stock_spot。
        return _round_spot(code)
    return await db.scalar(select(StockSpot).where(StockSpot.code == code))


def _all_round_spots() -> list:
    payload = _quote_round_context()
    return [
        SimpleNamespace(**item)
        for item in (payload.get("records") or [])
        if isinstance(item, dict) and item.get("code")
    ]


async def _spots_by_codes(db: AsyncSession, codes: set[str] | list[str]) -> list:
    normalized = {str(code) for code in codes if code}
    payload = _quote_round_context()
    if payload.get("round_id"):
        return [spot for code in normalized if (spot := _round_spot(code)) is not None]
    return list(
        (
            await db.scalars(select(StockSpot).where(StockSpot.code.in_(normalized)))
        ).all()
    )


def _active_blacklist_clause(trade_day: date):
    """Return the causal validity window for one requested trading date."""

    return and_(
        StockBlacklist.start_date <= trade_day,
        or_(
            StockBlacklist.end_date.is_(None),
            StockBlacklist.end_date >= trade_day,
        ),
    )


async def _trade_day_hold_days(start_date: date, end_date: date) -> int:
    """按交易日历计算持仓天数；买入日为0，之后每个交易日加1。"""
    if end_date <= start_date:
        return 0
    days = await trade_calendar.trade_days_between(
        start_date + timedelta(days=1),
        end_date,
    )
    return len(days)


def _execution_quote_status(
    spot: Optional[StockSpot],
    trade_date: date,
    *,
    now: Optional[datetime] = None,
) -> tuple[bool, str]:
    """按源/接收/提交三套时钟验收，并强制匹配当前不可变行情轮次。"""
    if spot is None:
        return False, "缺少当前行情轮次报价，禁止发送模拟委托"
    now = now or _paper_now()
    round_context = _quote_round_context()
    expected_round_id = str(round_context.get("round_id") or "")
    actual_round_id = str(getattr(spot, "quote_round_id", "") or "")
    if expected_round_id and actual_round_id != expected_round_id:
        return False, (
            f"行情轮次不一致：expected={expected_round_id}，actual={actual_round_id or 'missing'}"
        )

    timestamps = {
        "source_quote_at": getattr(spot, "source_quote_at", None),
        "received_at": getattr(spot, "received_at", None),
        "committed_at": getattr(spot, "updated_at", None),
    }
    if expected_round_id and not isinstance(timestamps["source_quote_at"], datetime):
        return False, "当前轮次该股票缺少源行情时间，失败关闭"
    effective = next(
        (value for value in timestamps.values() if isinstance(value, datetime)),
        None,
    )
    if effective is None:
        return False, "实时行情缺少源/接收/提交时间，禁止发送模拟委托"
    max_age = max(1, int(getattr(settings, "PAPER_EXECUTION_QUOTE_MAX_AGE_SEC", 90)))
    for label, timestamp in timestamps.items():
        if not isinstance(timestamp, datetime):
            continue
        if timestamp.date() != trade_date:
            return False, f"{label}日期{timestamp.date()}不是交易日{trade_date}"
        age_seconds = (now - timestamp).total_seconds()
        if age_seconds < -10:
            return False, f"{label}超前{abs(age_seconds):.0f}秒，时钟异常"
        if age_seconds > max_age:
            return False, f"{label}已过期{age_seconds:.0f}秒，超过{max_age}秒硬门槛"
    price = _to_float(getattr(spot, "price", None))
    if price is None or price <= 0:
        return False, "实时行情价格无效，禁止发送模拟委托"
    return True, ""


def _stable_intraday_entry_quote(
    spot: Optional[StockSpot], *, account_name: str = PAPER_ACCOUNT_DEFAULT,
) -> tuple[bool, str, dict]:
    """入场数据与分时策略过滤；高点回撤阈值不是交易所T+1规则。"""
    if spot is None:
        return False, "缺少实时行情", {}
    price = _to_float(getattr(spot, "price", None))
    avg_price = _to_float(getattr(spot, "avg_price", None))
    high_price = _to_float(getattr(spot, "high", None))
    limit_up = _to_float(getattr(spot, "limit_up", None))
    if price is None or price <= 0:
        return False, "现价无效", {}
    if limit_up is None or limit_up <= 0:
        return False, "涨停价缺失，无法验证可成交边界", {}
    if avg_price is None or avg_price <= 0:
        return False, "VWAP缺失，不能确认持续承接", {}
    if price < avg_price:
        return False, f"现价{price:.2f}低于VWAP{avg_price:.2f}", {}
    if high_price is None or high_price <= 0:
        return False, "日内高点缺失，不能识别冲高回落", {}
    pullback_pct = max(0.0, (high_price - price) / high_price * 100.0)
    max_pullback = max(
        0.0,
        float(account_confirmation_policy(account_name)["max_pullback_from_high_pct"]),
    )
    if pullback_pct > max_pullback:
        return False, f"较日内高点回落{pullback_pct:.2f}%超过{max_pullback:.2f}%", {}
    return True, "", {
        "price": price,
        "avg_price": avg_price,
        "high_price": high_price,
        "limit_up": limit_up,
        "pullback_from_high_pct": round(pullback_pct, 4),
    }


async def _pending_primary_buy_confirmation(
    db, *, account_name: str, source: str, candidate: dict, spot,
    limit_price: float, now: datetime,
) -> tuple[str, str]:
    """Recheck existing entry predicates; never refresh the frozen buy signal."""
    if (candidate.get("code") != getattr(spot, "code", None)
            or candidate.get("_source") != source):
        return "canceled", "原始候选与本轮代码/来源不一致"
    # No stale candidate-price fallback on a fill round.
    required = ["price", "prev_close", "avg_price", "high", "limit_up"]
    if account_name == PAPER_ACCOUNT_DEFAULT:
        required += ["low"]
    for key in required:
        value = _to_float(getattr(spot, key, None))
        if value is None or value <= 0:
            return "waiting", f"暂缺成交前有效{key}证据"
    if float(spot.high) < float(spot.price):
        return "waiting", "暂缺有效日内高点：高点低于现价"
    stable, reason, _ = _stable_intraday_entry_quote(spot, account_name=account_name)
    if not stable:
        return "canceled", f"原分时确认已失效：{reason}"
    diagnostics: list[dict] = []
    def empty_candidate_status() -> str:
        # Use the existing structured diagnostic, never infer recoverability
        # from a Chinese reason substring or permit a trade on missing data.
        relevant = [d for d in diagnostics if d.get("code") in (None, candidate["code"])]
        if relevant and all(
            d.get("stage_code") == "data_gate"
            and ((isinstance(d.get("candidate"), dict)
                  and d["candidate"].get("recoverable") is True)
                 or d.get("reason_code") == "prediction_not_visible")
            for d in relevant
        ):
            return "waiting"
        return "canceled"

    if source != "next_day_plan" and _to_float(getattr(spot, "change_pct", None)) is None:
        return "waiting", "暂缺成交前涨幅证据"
    if source.startswith("promotion_"):
        if source != f"promotion_{account_name}" or account_name not in PAPER_PROMOTION_ACCOUNTS:
            return "canceled", "原晋级路线与执行账户不一致"
        if _to_float(getattr(spot, "volume_ratio", None)) is None:
            return "waiting", "暂缺成交前量比证据"
        rows, notes = await _promotion_route_buy_candidates(
            db, limit=1, trade_date=now.date(), account_name=account_name,
            now=now, only_code=str(candidate["code"]), diagnostics=diagnostics)
        if not rows:
            return empty_candidate_status(), "原晋级买点当前治理/实时确认未通过：" + "；".join(notes)
        if rows[0].get("prediction_run_key") != candidate.get("prediction_run_key"):
            return "canceled", "晋级治理批次已替换，旧委托不得消费新批次身份"
        return "valid", ""
    if source == "tenbagger_midline":
        if account_name not in {PAPER_ACCOUNT_TENBAGGER, PAPER_ACCOUNT_CHALLENGER_E}:
            return "canceled", "高标路线与执行账户不一致"
        rows, notes = await _tenbagger_midline_candidates(
            db, limit=1, trade_date=now.date(), account_name=account_name,
            only_code=str(candidate["code"]), diagnostics=diagnostics)
    elif source == "reversal_pullback":
        if account_name != PAPER_ACCOUNT_REVERSAL:
            return "canceled", "反包路线与执行账户不一致"
        rows, notes = await _reversal_pullback_candidates(
            db, limit=1, trade_date=now.date(), only_code=str(candidate["code"]))
    elif account_name == PAPER_ACCOUNT_DEFAULT:
        current = dict(candidate)
        reason = await _confirm_candidate_main_fund(
            db, current, spot, trade_date=now.date(), decision_at=now)
        if reason:
            return ("waiting" if "unknown" in reason else "canceled"), reason
        if _a_entry_price_band(spot).get("status") == "empty":
            return "canceled", "A原入口必要价格区间已无交集"
        reason = _candidate_execution_value_reject_reason(
            current, price=limit_price, spot=spot)
        if reason:
            return "canceled", reason
        current["change_pct"] = _to_float(getattr(spot, "change_pct", None))
        reason = await _continuation_risk_reject_reason(
            db, code=str(candidate["code"]), trade_date=now.date(), candidate=current)
        return ("canceled", reason) if reason else ("valid", "")
    else:
        return "canceled", "无可验证的原账户买入路线，失败关闭"
    if not rows:
        return empty_candidate_status(), "原形态路线当前实时确认未通过：" + "；".join(notes)
    if rows[0].get("signal_date") != candidate.get("signal_date"):
        return "canceled", "原形态信号日期已替换，旧委托不得消费新信号"
    return "valid", ""


def _confirmation_streak_status(
    sample_times: list[datetime],
    *,
    current_at: datetime,
    min_samples: int,
    min_persistence_sec: int,
    max_sample_gap_sec: int,
    clock_jitter_sec: float | None = None,
) -> tuple[bool, int, float]:
    """仅计算截至current_at的连续、不重复报价序列，不用未来时点。"""
    unique = sorted({item for item in sample_times if isinstance(item, datetime) and item <= current_at})
    if current_at not in unique:
        unique.append(current_at)
    streak = [unique[-1]]
    for item in reversed(unique[:-1]):
        gap = (streak[0] - item).total_seconds()
        if gap <= 0:
            continue
        if gap > max(max_sample_gap_sec, 1):
            break
        streak.insert(0, item)
    duration = max(0.0, (streak[-1] - streak[0]).total_seconds())
    clock_jitter = max(
        0.0,
        float(settings.PAPER_CONFIRMATION_CLOCK_JITTER_SEC if clock_jitter_sec is None else clock_jitter_sec),
    )
    ready = (
        len(streak) >= max(min_samples, 1)
        and duration + clock_jitter >= max(min_persistence_sec, 0)
    )
    return ready, len(streak), duration


async def _champion_intraday_confirmation_status(
    db: AsyncSession,
    *,
    account_id: int,
    trade_date: date,
    code: str,
    source: str,
    current_at: datetime,
    account_name: str = PAPER_ACCOUNT_DEFAULT,
) -> tuple[bool, int, float]:
    """只恢复本账户当前版本且当时已提交的确认；旧版本不为新参数凑帧数。"""
    policy = account_confirmation_policy(account_name)
    min_samples = max(int(policy["min_samples"]), 1)
    max_gap = max(int(policy["max_sample_gap_sec"]), 1)
    rows = list(
        (
            await db.scalars(
                select(PaperAutoTradeLog)
                .where(
                    PaperAutoTradeLog.account_id == account_id,
                    PaperAutoTradeLog.trade_date == trade_date,
                    PaperAutoTradeLog.code == code,
                    PaperAutoTradeLog.source == source,
                    PaperAutoTradeLog.action == "confirm_buy",
                    PaperAutoTradeLog.strategy_version == _strategy_version(account_name),
                    PaperAutoTradeLog.created_at <= current_at,
                )
                .order_by(desc(PaperAutoTradeLog.id))
                .limit(max(min_samples * 4, 8))
            )
        ).all()
    )
    sample_times: list[datetime] = []
    for row in rows:
        payload = _json_loads_dict(row.candidate_json)
        if payload.get("confirmation_version") != "champion_persistent_v1":
            continue
        parsed = payload.get("confirmation_sample_at")
        try:
            sample_at = datetime.fromisoformat(str(parsed))
        except (TypeError, ValueError):
            sample_at = row.created_at if isinstance(row.created_at, datetime) else None
        if sample_at is not None:
            sample_times.append(sample_at)
    return _confirmation_streak_status(
        sample_times,
        current_at=current_at,
        min_samples=min_samples,
        min_persistence_sec=max(
            int(policy["min_persistence_sec"]),
            0,
        ),
        max_sample_gap_sec=max_gap,
        clock_jitter_sec=policy["clock_jitter_sec"],
    )


async def _strategy_mid_session_enable_block_reason(
    db: AsyncSession,
    *,
    account_id: int,
    trade_date: date,
) -> str:
    """当日已经以暂停态运行过时，盘中重启/改配置只能从下一交易日开始成交。"""
    if not settings.PAPER_AUTO_ORDER_ENABLE_NEXT_SESSION_ONLY:
        return ""
    paused_log_id = await db.scalar(
        select(PaperAutoTradeLog.id)
        .where(
            PaperAutoTradeLog.account_id == account_id,
            PaperAutoTradeLog.trade_date == trade_date,
            PaperAutoTradeLog.decision == "dry_run",
            PaperAutoTradeLog.reason.like("该策略自动买入委托已因校正证据暂停%"),
        )
        .limit(1)
    )
    if paused_log_id is None:
        return ""
    return "本交易日早先处于自动买入暂停态；盘中启用仅从下一交易日生效，当前继续演练"


def _conservative_execution_price(spot: StockSpot, side: str) -> Optional[float]:
    """用一档盘口与固定不利滑点生成保守限价，封死无盘口时拒绝成交。"""
    side = str(side or "").lower()
    last = _to_float(getattr(spot, "price", None))
    if last is None or last <= 0 or side not in {"buy", "sell"}:
        return None
    raw_slippage = getattr(settings, "PAPER_EXECUTION_SLIPPAGE_PCT", 0.10)
    slippage_pct = _to_float(0 if raw_slippage is None else raw_slippage)
    if slippage_pct is None:
        return None
    slippage = max(0.0, slippage_pct) / 100.0
    # A missing level may use the original last-price fallback; an invalid
    # supplied level/limit may not masquerade as missing evidence.
    book_key, limit_key = ("ask1_price", "limit_up") if side == "buy" else ("bid1_price", "limit_down")
    for key in (book_key, limit_key):
        raw_value = getattr(spot, key, None)
        if raw_value is not None and _to_float(raw_value) is None:
            return None
    if side == "buy":
        ask1 = _to_float(getattr(spot, "ask1_price", None))
        limit_up = _to_float(getattr(spot, "limit_up", None))
        if (ask1 is None or ask1 <= 0) and limit_up and last >= limit_up * 0.998:
            return None
        base = max(last, ask1) if ask1 and ask1 > 0 else last
        scaled_price = base * (1.0 + slippage) * 100.0 - 1e-9
        if not math.isfinite(scaled_price) or scaled_price <= 0:
            return None
        value = math.ceil(scaled_price) / 100.0
        result = round(min(value, limit_up), 2) if limit_up and limit_up > 0 else round(value, 2)
        return result if result > 0 else None

    bid1 = _to_float(getattr(spot, "bid1_price", None))
    limit_down = _to_float(getattr(spot, "limit_down", None))
    if (bid1 is None or bid1 <= 0) and limit_down and last <= limit_down * 1.002:
        return None
    base = min(last, bid1) if bid1 and bid1 > 0 else last
    scaled_price = base * (1.0 - slippage) * 100.0 + 1e-9
    if slippage >= 1 or not math.isfinite(scaled_price) or scaled_price <= 0:
        return None
    value = math.floor(scaled_price) / 100.0
    result = round(max(value, limit_down), 2) if limit_down and limit_down > 0 else round(value, 2)
    return result if result > 0 else None


def _is_limit_up_queue_quote(spot: Optional[StockSpot]) -> bool:
    """是否为可报涨停价队列、但不能直接模拟成交的封板盘口。"""
    if spot is None:
        return False
    last = _to_float(getattr(spot, "price", None))
    limit_up = _to_float(getattr(spot, "limit_up", None))
    ask1 = _to_float(getattr(spot, "ask1_price", None))
    bid1 = _to_float(getattr(spot, "bid1_price", None))
    if getattr(spot, "ask1_price", None) is not None and ask1 is None:
        return False  # Invalid supplied ask must not become a sealed-queue signal.
    return bool(
        last
        and limit_up
        and round(last, 2) >= round(limit_up, 2)
        and (ask1 is None or ask1 <= 0)
        and bid1
        and round(bid1, 2) >= round(limit_up, 2)
    )


def _effective_board_tag(code: str, tag: Optional[StockTag]) -> str:
    """标签缺失时按代码板块保守推导，未知/创新板只观察而非默认可交易。"""
    if tag and tag.board_tag:
        return str(tag.board_tag)
    board_type = stock_tagger.get_board_type(code)
    return stock_tagger.get_board_tag(board_type)


def _miss_reason_category(reason: str, *, decision: str = "", action: str = "") -> str:
    """把自由文本决策归入稳定复盘类别，避免“没买到”只能人工逐条阅读。"""
    text_value = str(reason or "").lower()
    if any(token in text_value for token in ("质量", "完整度", "数据降级", "成交额缺失", "watermark")):
        return "data_quality"
    if any(token in text_value for token in ("竞价", "auction", "可撤单阶段")):
        return "auction_quality"
    if any(token in text_value for token in ("过期", "时间戳", "行情日期", "缺少实时行情", "时钟异常")):
        return "quote_stale"
    if any(token in text_value for token in ("持续确认", "等待分时", "等待下一次轮询", "确认区间")):
        return "confirmation_wait"
    if any(token in text_value for token in ("市场走弱", "涨跌比", "指数", "冰点", "午后未满足强势")):
        return "market_regime"
    if any(token in text_value for token in ("风控", "黑名单", "st股", "停牌", "退市")):
        return "risk"
    if any(token in text_value for token in ("容量", "日限", "最大持仓", "可用资金", "不足一手")):
        return "position_limit"
    if any(token in text_value for token in ("追高", "高点回落", "性价比", "涨停", "不可成交", "排队")):
        return "price_or_liquidity"
    if any(token in text_value for token in ("暂停", "演练", "下一交易日生效")) or decision == "dry_run":
        return "strategy_paused"
    if action == "empty":
        return "no_candidate"
    return "other"


def _auto_log_payload(log: PaperAutoTradeLog) -> dict:
    candidate = _json_loads_dict(log.candidate_json)
    return {
        "id": log.id,
        "run_id": log.run_id,
        "trade_date": log.trade_date.isoformat() if log.trade_date else None,
        "created_at": log.created_at.isoformat(sep=" ") if log.created_at else None,
        "trigger": log.trigger,
        "source": log.source,
        "code": log.code,
        "name": log.name,
        "action": log.action,
        "decision": log.decision,
        "reason": log.reason,
        "price": log.price,
        "amount": log.amount,
        "candidate_score": log.candidate_score,
        "risk_level": log.risk_level,
        "executed_trade_id": log.executed_trade_id,
        "strategy_version": getattr(log, "strategy_version", None),
        "quote_round_id": getattr(log, "quote_round_id", None),
        "as_of_at": (
            log.as_of_at.isoformat(sep=" ")
            if isinstance(getattr(log, "as_of_at", None), datetime)
            else None
        ),
        "stage_code": getattr(log, "stage_code", None),
        "reason_code": getattr(log, "reason_code", None),
        "metric_value": getattr(log, "metric_value", None),
        "threshold_value": getattr(log, "threshold_value", None),
        "config_version": getattr(log, "config_version", None),
        "code_version": getattr(log, "code_version", None),
        "miss_reason_category": (
            None
            if log.decision == "executed"
            else _miss_reason_category(log.reason or "", decision=log.decision or "", action=log.action or "")
        ),
        "signal_source": candidate.get("signal_source") or _PAPER_BUY_SOURCE_LABELS.get(log.source, log.source),
        "sector_name": candidate.get("sector_name") or candidate.get("driver_primary") or "",
        "stop_loss_price": candidate.get("stop_loss_price"),
        "confirmation_evidence": confirmation_evidence(candidate),
    }


_PAPER_TRADE_REASON_LABELS = {
    "take_profit": "止盈卖出",
    "stop_loss": "止损卖出",
    "manual_buy": "手动买入",
    "manual_sell": "手动卖出",
}
_PAPER_TRADE_SIGNAL_PREFIX_LABELS = (
    ("auto-promotion_promotion-", "晋级二板自动买入"),
    ("auto-promotion_mainline-", "主线扩散首板自动买入"),
    ("auto-promotion_auction-", "竞价高开强攻自动买入"),
    ("auto-tenbagger_midline-", "连板高标接力自动买入"),
    ("auto-reversal_pullback-", "断板反包自动买入"),
    ("auto-next_day_plan-", "高胜率预案自动买入"),
    ("auto-green_limit_reversal-", "绿盘转强自动买入"),
    ("auto-underwater_reversal-", "水下翻红自动买入"),
    ("auto-anomaly_buy_point-", "异动买点自动买入"),
    ("auto-ma5_pullback-", "5日线回踩自动买入"),
    ("auto-daily_participation-", "盘面滚动观察自动买入"),
    ("auto-icepoint_reversal-", "冰点转强自动买入"),
    ("auto-t-buyback-", "日内做T回补"),
    ("auto-sell-", "自动卖出"),
    ("auto-radar-", "牛股雷达自动买入"),
    ("vp-uptrend-close-", "量价上升趋势收盘确认买入"),
)


def _contains_chinese_text(value: str) -> bool:
    return any("\u4e00" <= char <= "\u9fff" for char in str(value or ""))


def _paper_trade_reason_zh(
    trade: PaperTradeLog,
    auto_log: Optional[PaperAutoTradeLog] = None,
) -> str:
    """交易记录只展示可读中文；原始 signal_id/reason 仍在独立字段保留追踪。"""
    if auto_log is not None:
        detail = str(auto_log.reason or "").strip()
        candidate = _json_loads_dict(auto_log.candidate_json)
        source_label = str(
            candidate.get("signal_source")
            or _PAPER_BUY_SOURCE_LABELS.get(str(auto_log.source or ""), "")
            or ""
        ).strip()
        if detail and _contains_chinese_text(detail):
            if source_label and source_label not in detail:
                return f"{source_label}：{detail}"
            return detail
        if source_label and _contains_chinese_text(source_label):
            action = "买入" if trade.trade_type == "buy" else "卖出"
            return f"{source_label}自动{action}"

    raw_values = (
        str(trade.reason or "").strip(),
        str(trade.signal_id or "").strip(),
    )
    for raw_value in raw_values:
        if raw_value and _contains_chinese_text(raw_value):
            return raw_value

    for raw_value in raw_values:
        normalized = raw_value.lower()
        if not normalized:
            continue
        if normalized in _PAPER_TRADE_REASON_LABELS:
            return _PAPER_TRADE_REASON_LABELS[normalized]
        for prefix, label in _PAPER_TRADE_SIGNAL_PREFIX_LABELS:
            if normalized.startswith(prefix):
                return label

    return "手动买入信号" if trade.trade_type == "buy" else "手动卖出原因"


def _paper_trade_pnl_context(
    trades: list[PaperTradeLog],
) -> tuple[dict[int, float], set[int]]:
    """按现有加权成本口径回放，返回卖出已实现收益率及当前持仓对应买入记录。"""
    state: dict[str, dict] = {}
    realized_pct_by_id: dict[int, float] = {}
    for trade in sorted(trades, key=lambda item: (item.trade_time, item.id or 0)):
        code = str(trade.code or "")
        amount = int(trade.amount or 0)
        if not code or amount <= 0:
            continue
        bucket = state.setdefault(code, {"amount": 0, "avg_price": 0.0, "buy_ids": set()})
        if trade.trade_type == "buy":
            if bucket["amount"] <= 0:
                bucket.update({"amount": 0, "avg_price": 0.0, "buy_ids": set()})
            old_value = float(bucket["avg_price"]) * int(bucket["amount"])
            new_amount = int(bucket["amount"]) + amount
            bucket["avg_price"] = (old_value + float(trade.price or 0) * amount) / new_amount
            bucket["amount"] = new_amount
            if trade.id is not None:
                bucket["buy_ids"].add(int(trade.id))
            continue
        if trade.trade_type != "sell":
            continue

        avg_price = float(bucket["avg_price"] or 0)
        realized_pnl = trade.realized_pnl
        if trade.id is not None and realized_pnl is not None and avg_price > 0:
            cost_basis = avg_price * amount
            if cost_basis > 0:
                realized_pct_by_id[int(trade.id)] = round(float(realized_pnl) / cost_basis * 100, 2)
        bucket["amount"] = int(bucket["amount"]) - amount
        if bucket["amount"] <= 0:
            bucket.update({"amount": 0, "avg_price": 0.0, "buy_ids": set()})

    open_buy_ids = {
        int(trade_id)
        for bucket in state.values()
        if int(bucket["amount"]) > 0
        for trade_id in bucket["buy_ids"]
    }
    return realized_pct_by_id, open_buy_ids


def _auto_log_stage_code(action: str, source: str) -> str:
    if action in {"hold", "sell", "skip_sell", "queue_sell", "deferred_sell"} or source in {"position", "position-t"}:
        return "position_risk"
    if action == "confirm_buy":
        return "quote_confirmation"
    if action in {"buy", "queue_buy", "deferred_buy", "skip_buy"}:
        return "entry_decision"
    if source == "candidate":
        return "candidate_scan"
    return "runtime_gate"


def _sell_log_reason(candidate: dict, outcome_reason: str) -> str:
    """把卖出触发原因前置到日志 reason，便于单字段审计。

    2026-09-17 复盘修复：卖出日志的 reason 原先只写执行管道文案
    （"已通过风控，等待下一健康行情轮次按五档深度撮合" / "下一轮按五档深度全部成交"），
    真实触发原因只存在于 candidate_json.exit_trigger_reason，审计必须跨字段才可读。
    结构化字段保持不变，仍以 candidate_json 为准。
    """
    trigger_reason = str((candidate or {}).get("exit_trigger_reason") or "").strip()
    text = str(outcome_reason or "")
    if trigger_reason and trigger_reason not in text:
        return f"{trigger_reason}；{text}"
    return text


# 2026-09-17 复盘修复：A股T+1 是"当日买入不可卖"的**静态已知状态**，
# 每个行情轮次重复落一条 skip_sell 只制造噪音而不增加信息
# （生产个案：账户3 600105 单日 3,028 条 "A股T+1：当天买入不能当天卖出"）。
# 现按 (账户, 股票, 交易日, 原因) 只落一条，并在状态持续时按刷新间隔更新
# 同一条的"末次复现"时间，既保留首末次证据，又把写入量从每轮一次降到每日 ≤10 次。
# 原因文本变化（例如出现强制退出原因）仍会另落一条。
#
# 实现保持**无状态**：不以进程内缓存做去重，避免跨数据库/跨测试的同键误命中，
# 也避免进程重启后重复落条。定位键使用稳定的 reason_code，文本键使用剥离
# "日内持续"后缀后的原始原因。
T1_SKIP_REASON_CODE = "t_plus_one_blocked"
_T1_PERSIST_SUFFIX_RE = re.compile(
    r"（日内持续：首次 \d{2}:\d{2}:\d{2}，末次 \d{2}:\d{2}:\d{2}）$"
)


def _t1_base_reason(text: str) -> str:
    """剥离"日内持续"刷新后缀，得到用于同键判定的原始原因文本。"""
    return _T1_PERSIST_SUFFIX_RE.sub("", str(text or ""))


async def _log_t1_skip_once(
    db: AsyncSession,
    *,
    run_id: str,
    trade_date: date,
    trigger: str,
    source: str,
    code: str,
    name: str,
    reason: str,
    price: Optional[float],
    amount: Optional[int],
    candidate: Optional[dict],
    account_id: Optional[int],
) -> Optional[PaperAutoTradeLog]:
    """同一 T+1 阻塞状态只在首次落库，其后按刷新间隔更新末次复现时间。

    返回 None 表示本轮被去重抑制，调用方不应再追加日志。
    """
    now = _paper_now()
    rows = (await db.scalars(
        select(PaperAutoTradeLog)
        .where(
            PaperAutoTradeLog.account_id == account_id,
            PaperAutoTradeLog.code == code,
            PaperAutoTradeLog.trade_date == trade_date,
            PaperAutoTradeLog.action == "skip_sell",
            PaperAutoTradeLog.reason_code == T1_SKIP_REASON_CODE,
        )
        .order_by(PaperAutoTradeLog.id)
    )).all()
    row = next((item for item in rows if _t1_base_reason(item.reason) == str(reason)), None)
    if row is None:
        return await _add_auto_log(
            db, run_id=run_id, trade_date=trade_date, trigger=trigger,
            source=source, action="skip_sell", decision="skipped",
            reason=reason, code=code, name=name, price=price,
            amount=amount, candidate=candidate, account_id=account_id,
            reason_code=T1_SKIP_REASON_CODE,
        )
    refresh_sec = float(getattr(settings, "PAPER_T1_SKIP_LOG_REFRESH_MIN_SEC", 1800.0))
    last_write_at = row.as_of_at or row.created_at or now
    first_at = row.created_at or now
    if refresh_sec > 0 and (now - last_write_at).total_seconds() < refresh_sec:
        return None
    row.reason = (
        f"{str(reason)}（日内持续：首次 {first_at.strftime('%H:%M:%S')}，"
        f"末次 {now.strftime('%H:%M:%S')}）"
    )
    row.as_of_at = now
    await db.flush()
    return None


async def _add_auto_log(
    db: AsyncSession,
    *,
    run_id: str,
    trade_date: date,
    trigger: str,
    source: str,
    action: str,
    decision: str,
    reason: str,
    code: str = "",
    name: str = "",
    price: Optional[float] = None,
    amount: Optional[int] = None,
    candidate_score: Optional[float] = None,
    risk_level: str = "",
    risk: Optional[dict] = None,
    candidate: Optional[dict] = None,
    executed_trade_id: Optional[int] = None,
    account_id: Optional[int] = None,
    strategy_version: str = "",
    stage_code: str = "",
    reason_code: str = "",
    metric_value: Optional[float] = None,
    threshold_value: Optional[float] = None,
    created_at: Optional[datetime] = None,
    deferred_buy_outcome: Optional[dict] = None,
) -> PaperAutoTradeLog:
    round_context = _quote_round_context()
    candidate_payload = dict(candidate or {})
    if deferred_buy_outcome is not None:
        try:
            candidate_payload.update(deferred_buy_log_evidence(
                candidate_payload, outcome=deferred_buy_outcome, code=code,
                evaluated_at=created_at,
            ))
        except Exception as exc:
            # A failed optional observation must not change an executed order/log.
            candidate_payload["confirmation_evidence"] = confirmation_evidence({})
            candidate_payload["deferred_order_observation"] = {
                "status": "unavailable", "error_type": type(exc).__name__, "replay_ready": False,
            }
            logger.warning("Deferred buy confirmation observation unavailable (%s)", type(exc).__name__)
    elif "confirmation_evidence" in candidate_payload:
        candidate_payload["confirmation_evidence"] = freeze_log_evidence(
            candidate_payload, action=action, decision=decision,
        )
    if round_context.get("round_id"):
        candidate_payload.setdefault("quote_round_id", round_context.get("round_id"))
        candidate_payload.setdefault("as_of_at", round_context.get("as_of_at"))
        candidate_payload.setdefault(
            "component_watermarks",
            _json_loads_dict(round_context.get("component_watermarks_json")),
        )
    resolved_strategy_version = str(
        strategy_version or _AUTO_STRATEGY_VERSION_CONTEXT.get() or ""
    )
    if not resolved_strategy_version and account_id:
        account_name = await db.scalar(
            select(PaperAccount.account_name).where(PaperAccount.id == account_id)
        )
        if account_name:
            resolved_strategy_version = _strategy_version(str(account_name))
    if not resolved_strategy_version:
        resolved_strategy_version = str(
            candidate_payload.get("strategy_version")
            or candidate_payload.get("route_version")
            or ""
        )
    resolved_stage = stage_code or _auto_log_stage_code(action, source)
    resolved_reason_code = reason_code or _miss_reason_category(
        reason,
        decision=decision,
        action=action,
    )
    log = PaperAutoTradeLog(
        run_id=run_id,
        trade_date=trade_date,
        created_at=created_at or _paper_now(),
        trigger=trigger,
        source=source,
        code=code or None,
        name=name or None,
        action=action,
        decision=decision,
        reason=reason,
        price=price,
        amount=amount,
        candidate_score=candidate_score,
        risk_level=risk_level or None,
        risk_json=_json_dumps(risk),
        candidate_json=_json_dumps(candidate_payload),
        executed_trade_id=executed_trade_id,
        account_id=account_id or None,
        strategy_version=resolved_strategy_version or None,
        quote_round_id=str(round_context.get("round_id") or "") or None,
        as_of_at=round_context.get("as_of_at") if isinstance(round_context.get("as_of_at"), datetime) else None,
        stage_code=resolved_stage,
        reason_code=resolved_reason_code,
        metric_value=metric_value,
        threshold_value=threshold_value,
        config_version=str(round_context.get("config_version") or "") or None,
        code_version=str(round_context.get("code_version") or "") or None,
    )
    db.add(log)
    await db.flush()
    return log


async def _position_context(db: AsyncSession, account: PaperAccount) -> tuple[dict, float]:
    positions = await _open_positions(db, account.id)
    current_positions = {}
    position_value = 0.0
    for position in positions:
        price = float(position.current_price or position.buy_price or 0)
        value = round(price * int(position.buy_amount or 0), 2)
        current_positions[position.code] = {
            "shares": position.buy_amount,
            "cost": position.buy_price,
            "market_value": value,
        }
        position_value += value
    return current_positions, round(position_value, 2)


async def _risk_check_for_buy(
    db: AsyncSession,
    account: PaperAccount,
    code: str,
    price: float,
    amount: int,
    *,
    recovery_probe: bool = False,
    continuous_participation_probe: bool = False,
) -> dict:
    tag = (await db.execute(select(StockTag).where(StockTag.code == code))).scalar_one_or_none()
    # === 2026-08-31 防御: stock_blacklist 命中即视为 ST (覆盖 600002 类静默通过) ===
    blacklist = (await db.execute(
        select(StockBlacklist).where(
            StockBlacklist.code == code,
            StockBlacklist.reason.in_(["st", "delisting", "suspended"]),
            _active_blacklist_clause(_paper_now().date()),
        )
    )).scalar_one_or_none()
    current_positions, position_value = await _position_context(db, account)
    today = _paper_now().date()
    sentiment_state = await sentiment_circuit_breaker.get_current_state(db, today)
    sentiment_quality_status, sentiment_quality_reason = sentiment_quality_at(
        sentiment_state, at=_paper_now(),
        strict=experiment_active(str(account.account_name or "default"), at=_paper_now()),
    )
    if sentiment_state.trade_date != today:
        sentiment_quality_status = "stale"
        sentiment_quality_reason = (
            f"请求{today.isoformat()}，仅有"
            f"{sentiment_state.trade_date.isoformat() if sentiment_state.trade_date else '无'}情绪快照"
        )
    in_experiment = experiment_active(str(account.account_name or "default"), at=_paper_now())
    # 旧模式保持兼容；实验不篡改实际回撤，统一风控链仅将绩效熔断改为记录。
    bypass_risk = not in_experiment and _drawdown_buy_gate_bypasses_risk()
    ctx = RiskContext(
        code=code,
        action="buy",
        price=price,
        amount=amount,
        total_assets=float(account.total_assets or account.initial_capital or 0),
        cash=float(account.current_capital or 0),
        position_value=position_value,
        # unlimited 模式强制把回撤置 0, 让 MaxDrawdownRule 直接放行;
        # 单笔仓位级风控（涨跌停/ST/停牌/单笔金额等）仍然由其它规则检查。
        max_drawdown=0.0 if bypass_risk else _current_account_drawdown(account),
        is_paper_experiment=in_experiment,
        sentiment_required=sentiment_is_required(str(account.account_name or "default")),
        is_drawdown_recovery_probe=recovery_probe or bypass_risk,
        drawdown_recovery_limit_pct=(
            0
            if bypass_risk or continuous_participation_probe
            else _drawdown_recovery_limit_pct()
            if recovery_probe
            else 0
        ),
        current_positions=current_positions,
        sentiment_cycle=str(sentiment_state.phase or "divergence"),
        sentiment_score=float(sentiment_state.score or 0),
        sentiment_quality_status=sentiment_quality_status,
        sentiment_quality_reason=sentiment_quality_reason,
        board_tag=_effective_board_tag(code, tag),
        is_st=bool(tag.is_st) if tag else bool(blacklist and blacklist.reason == "st"),
        is_suspended=bool(tag.is_suspended) if tag else bool(blacklist and blacklist.reason == "suspended"),
        is_delisting=bool(tag.is_delisting) if tag else bool(blacklist and blacklist.reason == "delisting"),
        is_ipo_recent=bool(tag.is_ipo_recent) if tag else False,
    )
    return risk_engine.check(ctx)


def _continuous_participation_warn_allowed(risk: dict, enabled: bool) -> bool:
    """深回撤分层观察仓只可穿透最大回撤警告，其他警告仍拦截。"""
    if not enabled or risk.get("final_level") != "warn":
        return False
    warnings = list(risk.get("warnings") or [])
    return bool(warnings) and all(
        str(item.get("rule") or "") == "max_drawdown"
        and str(item.get("category") or "") == "drawdown"
        for item in warnings
    )


def _auto_position_pct(score: float) -> float:
    if score >= 90:
        return settings.PAPER_AUTO_POSITION_PCT
    if score >= 85:
        return min(settings.PAPER_AUTO_POSITION_PCT, 0.45)
    if score >= 80:
        return min(settings.PAPER_AUTO_POSITION_PCT, 0.35)
    if score >= 76:
        return min(settings.PAPER_AUTO_POSITION_PCT, 0.30)
    return min(settings.PAPER_AUTO_POSITION_PCT, 0.25)


def _current_account_drawdown(account: PaperAccount) -> float:
    return abs(float(getattr(account, "current_drawdown", account.max_drawdown or 0) or 0))


def _drawdown_buy_gate_mode() -> str:
    """读取回撤开仓闸门总开关。pause/cautious/unlimited 三档."""
    return str(getattr(settings, "PAPER_AUTO_DRAWDOWN_BUY_GATE_MODE", "pause") or "pause").lower()


def _drawdown_buy_gate_bypasses_paper() -> bool:
    """回撤开仓闸门是否绕过 paper 层暂停逻辑.

    cautious/unlimited 都跳过 paper 层 (2%/3.5%/5% 的暂停/锁仓).
    """
    mode = _drawdown_buy_gate_mode()
    return mode in {"cautious", "unlimited"}


def _drawdown_buy_gate_bypasses_risk() -> bool:
    """回撤开仓闸门是否绕过 risk 层熔断 (15% 硬熔断).

    仅 unlimited 跳过. cautious 保留 risk 层熔断作为最后防线.
    """
    mode = _drawdown_buy_gate_mode()
    return mode == "unlimited"


def _drawdown_recovery_limit_pct() -> float:
    if not settings.PAPER_AUTO_DRAWDOWN_PERMANENT_LOCK_ENABLED:
        return 0.0
    return max(float(settings.PAPER_AUTO_DRAWDOWN_HARD_RECOVERY_MAX_PCT or 0), 0.0)


def _has_recovery_protected_profit_position(open_positions: list[PaperPosition]) -> bool:
    if not settings.PAPER_AUTO_DRAWDOWN_RECOVERY_ALLOW_PROFIT_POSITION:
        return False
    if not open_positions or len(open_positions) > settings.PAPER_AUTO_DRAWDOWN_RECOVERY_MAX_OPEN_POSITIONS:
        return False
    for position in open_positions:
        if int(position.buy_amount or 0) <= 0:
            continue
        profit_pct = _to_float(getattr(position, "profit_pct", None))
        if profit_pct is not None and profit_pct >= settings.PAPER_AUTO_DRAWDOWN_RECOVERY_MIN_HELD_PROFIT_PCT:
            return True
    return False


def _is_strong_market_recovery_sentiment(sentiment: Optional[MarketSentiment]) -> bool:
    if not settings.PAPER_AUTO_STRONG_MARKET_RECOVERY_ENABLED or not sentiment:
        return False
    return (
        int(sentiment.limit_up_count or 0) >= settings.PAPER_AUTO_STRONG_MARKET_RECOVERY_MIN_LIMIT_UP_COUNT
        and float(sentiment.advance_decline_ratio or 0) >= settings.PAPER_AUTO_STRONG_MARKET_RECOVERY_MIN_ADVANCE_DECLINE_RATIO
        and float(sentiment.main_net_inflow or 0) >= settings.PAPER_AUTO_STRONG_MARKET_RECOVERY_MIN_MAIN_NET_INFLOW
    )


async def _is_strong_market_recovery_day(db: AsyncSession, trade_date: date) -> bool:
    result = await db.execute(
        select(MarketSentiment).where(MarketSentiment.trade_date == trade_date)
    )
    return _is_strong_market_recovery_sentiment(result.scalar_one_or_none())


def _market_allows_daily_participation(sentiment: Optional[MarketSentiment]) -> bool:
    if not settings.PAPER_AUTO_DAILY_PARTICIPATION_ENABLED or not sentiment:
        return False
    return (
        int(sentiment.limit_up_count or 0) >= settings.PAPER_AUTO_DAILY_PARTICIPATION_MIN_LIMIT_UP_COUNT
        and int(sentiment.limit_down_count or 0) <= settings.PAPER_AUTO_DAILY_PARTICIPATION_MAX_LIMIT_DOWN_COUNT
        and float(sentiment.advance_decline_ratio or 0) >= settings.PAPER_AUTO_DAILY_PARTICIPATION_MIN_ADVANCE_DECLINE_RATIO
    )


def _market_is_icepoint_reversal_setup(sentiment: Optional[MarketSentiment]) -> bool:
    if not settings.PAPER_AUTO_ICEPOINT_REVERSAL_ENABLED or not sentiment:
        return False
    limit_down_count = int(sentiment.limit_down_count or 0)
    if limit_down_count >= settings.PAPER_AUTO_ICEPOINT_MELTDOWN_LIMIT_DOWN_COUNT:
        return False
    return (
        str(sentiment.sentiment_cycle or "") == "freezing"
        or int(sentiment.limit_up_count or 0) <= settings.PAPER_AUTO_ICEPOINT_LIMIT_UP_COUNT
        or limit_down_count >= settings.PAPER_AUTO_ICEPOINT_LIMIT_DOWN_COUNT
        or float(sentiment.advance_decline_ratio or 0) <= settings.PAPER_AUTO_ICEPOINT_MAX_ADVANCE_DECLINE_RATIO
    )


def _market_allows_afternoon_new_buy(sentiment: Optional[MarketSentiment]) -> bool:
    if not settings.PAPER_AUTO_AFTERNOON_NEW_BUY_STRONG_MARKET_ONLY:
        return True
    return _is_strong_market_recovery_sentiment(sentiment)


def _candidate_allows_afternoon_new_buy(
    sentiment: Optional[MarketSentiment],
    source: str,
    score: float,
) -> bool:
    if _market_allows_afternoon_new_buy(sentiment):
        return True
    if source == "daily_participation" and sentiment:
        # 每日参与候选已在当前快照完成量能/VWAP/低点回收确认，不再要求市场必须达到
        # “百股涨停”的强修复门槛；但跌停扩散或涨跌比极弱时仍不放行。
        return (
            score >= settings.PAPER_AUTO_DRAWDOWN_CONTINUOUS_PARTICIPATION_MIN_SCORE
            and int(sentiment.limit_down_count or 0)
            <= settings.PAPER_AUTO_DAILY_PARTICIPATION_MAX_LIMIT_DOWN_COUNT
            and float(sentiment.advance_decline_ratio or 0)
            >= settings.PAPER_AUTO_DAILY_PARTICIPATION_MIN_ADVANCE_DECLINE_RATIO
        )
    if not settings.PAPER_AUTO_AFTERNOON_LEADER_EXCEPTION_ENABLED or not sentiment:
        return False
    if source not in {"anomaly_buy_point", "underwater_reversal", "green_limit_reversal"}:
        return False
    return (
        score >= settings.PAPER_AUTO_AFTERNOON_LEADER_MIN_SCORE
        and int(sentiment.limit_up_count or 0) >= settings.PAPER_AUTO_AFTERNOON_LEADER_MIN_LIMIT_UP_COUNT
        and int(sentiment.limit_down_count or 0) <= settings.PAPER_AUTO_AFTERNOON_LEADER_MAX_LIMIT_DOWN_COUNT
        and float(sentiment.seal_rate or 0) >= settings.PAPER_AUTO_AFTERNOON_LEADER_MIN_SEAL_RATE
        and float(sentiment.advance_decline_ratio or 0)
        >= settings.PAPER_AUTO_AFTERNOON_LEADER_MIN_ADVANCE_DECLINE_RATIO
    )


def _candidate_allows_late_new_buy(
    now: Optional[datetime],
    sentiment: Optional[MarketSentiment],
    source: str,
    score: float,
) -> bool:
    if not settings.PAPER_AUTO_LATE_LEADER_EXCEPTION_ENABLED:
        return False
    now = now or datetime.now()
    end = _parse_hhmm(settings.PAPER_AUTO_LATE_LEADER_BUY_END, time(14, 40))
    if now.time() > end or trade_calendar.get_trade_session(now) != "afternoon":
        return False
    return _candidate_allows_afternoon_new_buy(sentiment, source, score)


async def _market_sentiment_for_date(db: AsyncSession, trade_date: date) -> Optional[MarketSentiment]:
    result = await db.execute(
        select(MarketSentiment).where(MarketSentiment.trade_date == trade_date)
    )
    return result.scalar_one_or_none()


def _is_drawdown_recovery_buy(
    account: PaperAccount,
    open_count: int,
    today_new_buy_count: int,
    candidate_score: float,
    allow_profit_position: bool = False,
    recovery_max_buys: Optional[int] = None,
    recovery_max_open_positions: Optional[int] = None,
    allow_continuous_participation: bool = False,
) -> bool:
    if experiment_active(str(account.account_name or "default"), at=_paper_now()):
        return False  # 持续实验用固定策略预算，不转入历史回撤恢复路线
    drawdown = _current_account_drawdown(account)
    max_buys = settings.PAPER_AUTO_DRAWDOWN_RECOVERY_MAX_BUYS if recovery_max_buys is None else recovery_max_buys
    max_open_positions = (
        settings.PAPER_AUTO_DRAWDOWN_RECOVERY_MAX_OPEN_POSITIONS
        if recovery_max_open_positions is None
        else recovery_max_open_positions
    )
    # === 2026-08-31 闸门总开关: cautious/unlimited 跳过 paper 层暂停/锁仓 ===
    if _drawdown_buy_gate_bypasses_paper():
        return True
    if drawdown < settings.PAPER_AUTO_DRAWDOWN_PAUSE_BUY_PCT:
        return False
    if not settings.PAPER_AUTO_DRAWDOWN_RECOVERY_ENABLED:
        return False
    if drawdown >= settings.PAPER_AUTO_DRAWDOWN_RECOVERY_HARD_PAUSE_PCT:
        if (
            allow_continuous_participation
            and settings.PAPER_AUTO_DRAWDOWN_CONTINUOUS_PARTICIPATION_ENABLED
            and open_count <= max_open_positions
            and today_new_buy_count
            < settings.PAPER_AUTO_DRAWDOWN_CONTINUOUS_PARTICIPATION_MAX_BUYS
            and candidate_score
            >= settings.PAPER_AUTO_DRAWDOWN_CONTINUOUS_PARTICIPATION_MIN_SCORE
        ):
            return True
        recovery_limit = _drawdown_recovery_limit_pct()
        return (
            settings.PAPER_AUTO_DRAWDOWN_HARD_RECOVERY_ENABLED
            and (recovery_limit <= 0 or drawdown < recovery_limit)
            and open_count < max_open_positions
            and today_new_buy_count < max_buys
            and candidate_score >= settings.PAPER_AUTO_DRAWDOWN_HARD_RECOVERY_MIN_SCORE
        )
    if open_count > 0 and not allow_profit_position:
        return False
    if open_count > max_open_positions:
        return False
    if today_new_buy_count >= max_buys:
        return False
    return candidate_score >= settings.PAPER_AUTO_DRAWDOWN_RECOVERY_MIN_SCORE


def _auto_buy_pause_reason(
    account: PaperAccount,
    open_count: int = 0,
    today_new_buy_count: int = 0,
    candidate_score: float = 0,
    allow_profit_position: bool = False,
    recovery_max_buys: Optional[int] = None,
    recovery_max_open_positions: Optional[int] = None,
    allow_continuous_participation: bool = False,
) -> str:
    if experiment_active(str(account.account_name or "default"), at=_paper_now()):
        return ""
    drawdown = _current_account_drawdown(account)
    # === 2026-08-31 闸门总开关: cautious/unlimited 跳过 paper 层暂停 ===
    if _drawdown_buy_gate_bypasses_paper():
        return ""
    if drawdown >= settings.PAPER_AUTO_DRAWDOWN_PAUSE_BUY_PCT:
        if _is_drawdown_recovery_buy(
            account,
            open_count=open_count,
            today_new_buy_count=today_new_buy_count,
            candidate_score=candidate_score,
            allow_profit_position=allow_profit_position,
            recovery_max_buys=recovery_max_buys,
            recovery_max_open_positions=recovery_max_open_positions,
            allow_continuous_participation=allow_continuous_participation,
        ):
            return ""
        recovery_requirement = "空仓/盈利保护持仓高分恢复开仓条件"
        return (
            f"账户当前回撤{drawdown:.2f}%达到暂停开仓线"
            f"{settings.PAPER_AUTO_DRAWDOWN_PAUSE_BUY_PCT:.2f}%，未满足{recovery_requirement}"
        )
    return ""


def _auto_buy_amount(account: PaperAccount, price: float, open_count: int, score: float = 0) -> int:
    if price <= 0:
        return 0
    remaining_slots = max(1, settings.PAPER_AUTO_MAX_POSITIONS - open_count)
    total_assets = float(account.total_assets or account.initial_capital or settings.PAPER_INITIAL_CAPITAL or 0)
    drawdown = _current_account_drawdown(account)
    in_experiment = experiment_active(str(account.account_name or "default"), at=_paper_now())
    if in_experiment:
        per_trade_budget = total_assets * _auto_position_pct(score)
    elif (
        settings.PAPER_AUTO_DRAWDOWN_RECOVERY_ENABLED
        and drawdown >= settings.PAPER_AUTO_DRAWDOWN_PAUSE_BUY_PCT
    ):
        recovery_pct = settings.PAPER_AUTO_DRAWDOWN_RECOVERY_POSITION_PCT
        if (
            settings.PAPER_AUTO_DRAWDOWN_HARD_RECOVERY_ENABLED
            and drawdown >= settings.PAPER_AUTO_DRAWDOWN_RECOVERY_HARD_PAUSE_PCT
            and score >= settings.PAPER_AUTO_DRAWDOWN_HARD_CONVICTION_MIN_SCORE
        ):
            recovery_pct = max(recovery_pct, settings.PAPER_AUTO_DRAWDOWN_HARD_CONVICTION_POSITION_PCT)
        per_trade_budget = total_assets * recovery_pct
    elif drawdown >= settings.PAPER_AUTO_DRAWDOWN_HALF_BUY_PCT:
        per_trade_budget = total_assets * _auto_position_pct(score) * 0.5
    else:
        per_trade_budget = total_assets * _auto_position_pct(score)
    min_layer_budget = total_assets * _auto_position_pct(settings.PAPER_AUTO_MIN_SCORE)
    reserve_budget = max(0, remaining_slots - 1) * min_layer_budget
    cash_budget = max(0.0, float(account.current_capital or 0) - reserve_budget)
    if open_count >= settings.PAPER_AUTO_MAX_POSITIONS - 1:
        budget = max(0.0, cash_budget * 0.98)
    else:
        budget = max(0.0, min(per_trade_budget, cash_budget) * 0.98)
    amount = int(budget // (price * 100)) * 100
    if (
        not in_experiment
        and settings.PAPER_AUTO_DRAWDOWN_RECOVERY_ENABLED
        and drawdown >= settings.PAPER_AUTO_DRAWDOWN_PAUSE_BUY_PCT
        and settings.PAPER_AUTO_DRAWDOWN_RECOVERY_MAX_AMOUNT > 0
    ):
        amount = min(amount, _round_lot(settings.PAPER_AUTO_DRAWDOWN_RECOVERY_MAX_AMOUNT))
    if (
        not in_experiment
        and settings.PAPER_AUTO_DRAWDOWN_HARD_RECOVERY_ENABLED
        and drawdown >= settings.PAPER_AUTO_DRAWDOWN_RECOVERY_HARD_PAUSE_PCT
    ):
        hard_cap = settings.PAPER_AUTO_DRAWDOWN_HARD_RECOVERY_MAX_AMOUNT
        if score >= settings.PAPER_AUTO_DRAWDOWN_HARD_CONVICTION_MIN_SCORE:
            hard_cap = max(hard_cap, settings.PAPER_AUTO_DRAWDOWN_HARD_CONVICTION_MAX_AMOUNT)
        if hard_cap > 0:
            amount = min(amount, _round_lot(hard_cap))
    # === 硬止损仓位上限：单笔触发硬止损时最大亏损 ≤ 总资产 × 0.02 ===
    max_loss_pct = float(getattr(settings, "PAPER_AUTO_HARD_STOP_MAX_LOSS_PCT", 2.0) or 0)
    stop_loss_pct = float(getattr(settings, "PAPER_AUTO_STOP_LOSS_PCT", 5.0) or 0)
    if total_assets > 0 and price > 0 and stop_loss_pct > 0 and max_loss_pct > 0:
        max_loss_amount = total_assets * (max_loss_pct / 100.0)
        cap_amount = _round_lot(max_loss_amount / (price * (stop_loss_pct / 100.0)))
        if cap_amount > 0:
            amount = min(amount, cap_amount)
    return amount


def _full_conviction_buy_amount(account: PaperAccount, price: float) -> int:
    """兼容旧配置；即使显式开启也不得突破统一分批建仓总仓上限。"""
    if price <= 0:
        return 0
    total_assets = float(account.total_assets or account.initial_capital or settings.PAPER_INITIAL_CAPITAL or 0)
    target_pct = min(
        settings.PAPER_AUTO_FULL_CONVICTION_POSITION_PCT,
        settings.PAPER_AUTO_STAGED_ENTRY_MAX_POSITION_PCT,
    )
    target_budget = total_assets * target_pct
    cash_budget = float(account.current_capital or 0)
    budget = max(0.0, min(target_budget, cash_budget) * 0.98)
    return int(budget // (price * 100)) * 100


def _is_full_conviction_candidate(candidate: dict, score: float) -> bool:
    if not settings.PAPER_AUTO_FULL_CONVICTION_ENABLED:
        return False
    if not candidate.get("ma5_pullback"):
        return False
    ma5_distance = _to_float(candidate.get("ma5_distance_pct"))
    change_pct = _to_float(candidate.get("change_pct"))
    volume_ratio = _to_float(candidate.get("volume_ratio"))
    return (
        score >= settings.PAPER_AUTO_FULL_CONVICTION_MIN_SCORE
        and ma5_distance is not None
        and ma5_distance <= settings.PAPER_AUTO_FULL_CONVICTION_MAX_MA5_DIST_PCT
        and change_pct is not None
        and change_pct <= settings.PAPER_AUTO_FULL_CONVICTION_MAX_CHANGE_PCT
        and volume_ratio is not None
        and volume_ratio >= settings.PAPER_AUTO_FULL_CONVICTION_MIN_VOLUME_RATIO
    )


def _round_lot(amount: float) -> int:
    parsed = _to_float(amount)
    if parsed is None:
        return 0
    # Keep exact integer rounding (not a float conversion of a large integer).
    try:
        return max(0, int(amount) // 100 * 100)
    except (TypeError, ValueError, OverflowError):
        return 0


def _staged_entry_buy_amount(
    account: PaperAccount,
    price: float,
    score: float,
    position: Optional[PaperPosition] = None,
) -> int:
    """按信号质量给一层仓位，保留后续加仓空间，且不突破单股目标上限。"""
    if price <= 0:
        return 0
    total_assets = float(account.total_assets or account.initial_capital or settings.PAPER_INITIAL_CAPITAL or 0)
    cash = float(account.current_capital or 0)
    if score >= 90:
        layer_pct = settings.PAPER_AUTO_STAGED_ENTRY_STRONG_PCT
    elif score >= 85:
        layer_pct = settings.PAPER_AUTO_STAGED_ENTRY_NORMAL_PCT
    else:
        layer_pct = settings.PAPER_AUTO_STAGED_ENTRY_TRIAL_PCT
    if _current_account_drawdown(account) >= settings.PAPER_AUTO_DRAWDOWN_RECOVERY_HARD_PAUSE_PCT:
        layer_pct *= settings.PAPER_AUTO_STAGED_ENTRY_DEEP_DRAWDOWN_FACTOR

    current_value = 0.0
    if position and int(position.buy_amount or 0) > 0:
        current_value = price * int(position.buy_amount or 0)
    max_position_budget = total_assets * settings.PAPER_AUTO_STAGED_ENTRY_MAX_POSITION_PCT
    remaining_position_budget = max(0.0, max_position_budget - current_value)
    layer_budget = total_assets * layer_pct
    budget = max(0.0, min(layer_budget, remaining_position_budget, cash * 0.98))
    amount = int(budget // (price * 100)) * 100
    return min(
        amount,
        _round_lot(settings.PAPER_AUTO_STAGED_ENTRY_MAX_SHARES_PER_ORDER),
    )


def _layered_auto_buy_amount(
    account: PaperAccount,
    *,
    price: float,
    open_count: int,
    score: float,
    position: Optional[PaperPosition] = None,
) -> int:
    """自动买入统一取基础预算与分层预算的较小值，任何来源都不能绕过。"""
    base_amount = _auto_buy_amount(account, price, open_count, score)
    staged_amount = _staged_entry_buy_amount(account, price, score, position)
    if base_amount < 100 or staged_amount < 100:
        return 0
    return min(base_amount, staged_amount)


def _scale_in_reject_reason(
    candidate: dict,
    *,
    position: PaperPosition,
    price: float,
    score: float,
    total_assets: float,
    bought_code_today: bool,
) -> str:
    """同股加仓只允许支撑回收后的下一层，不允许追涨或下跌摊平。"""
    if not settings.PAPER_AUTO_SCALE_IN_ENABLED:
        return "分批建仓未启用"
    if score < settings.PAPER_AUTO_SCALE_IN_MIN_SCORE:
        return f"评分{score:.1f}未达到追加层门槛{settings.PAPER_AUTO_SCALE_IN_MIN_SCORE:.1f}"
    if bought_code_today:
        return "今日已完成该股一层建仓，等待下一交易日再次确认"
    if not _candidate_allows_continuous_participation(candidate, score):
        return "当前不是已确认的回踩/低点回收信号，不追加仓位"
    cost = float(position.buy_price or 0)
    if cost <= 0 or price <= 0:
        return "缺少有效持仓成本或现价"
    cost_return_pct = (price / cost - 1) * 100
    if cost_return_pct < settings.PAPER_AUTO_SCALE_IN_MIN_COST_RETURN_PCT:
        return f"现价较成本{cost_return_pct:.2f}%，尚未确认止跌，不做下跌摊平"
    if cost_return_pct > settings.PAPER_AUTO_SCALE_IN_MAX_COST_RETURN_PCT:
        return f"现价较成本上涨{cost_return_pct:.2f}%，超过加仓追价上限"
    stop_loss = _candidate_stop_loss(candidate, price)
    held_stop = _to_float(position.stop_loss_price)
    active_stop = max(value for value in (stop_loss, held_stop or 0) if value is not None)
    if active_stop > 0 and price <= active_stop:
        return f"现价已触及持仓/候选止损{active_stop:.2f}，禁止加仓"
    position_pct = price * int(position.buy_amount or 0) / max(total_assets, 1.0)
    if position_pct >= settings.PAPER_AUTO_STAGED_ENTRY_MAX_POSITION_PCT:
        return f"当前单股仓位已达{position_pct * 100:.1f}%，不再加层"
    return ""


def _is_full_exit_reason(reason: str) -> bool:
    return reason.startswith((
        "昨日涨停次日转弱",
        "昨日涨停次日跌停",
        "触发持仓止损价",
        "触发硬止损",
        "跌破买入价小止损",
        "次日不强就走",
        "跌破5日线",
        "放量阴线",
        "板块退潮",
        "持仓",
    ))


def _is_post_t_protect_exit_reason(reason: str) -> bool:
    return reason.startswith((
        "回落成本线保护",
        "盘中冲高回落",
        "盘中收弱",
        "跌破分时均价",
        "跌破开盘价",
        "5分钟急跌",
        "盘口卖压增强",
        "跌破5日线",
        "板块退潮",
    ))


def _auto_sell_amount(position: PaperPosition, available_amount: int, reason: str, *, params: Optional[dict] = None) -> int:
    params = params or {}
    available_amount = _round_lot(available_amount)
    if available_amount < 100:
        return 0

    if not params.get("trade_t_enabled", settings.PAPER_AUTO_TRADE_T_ENABLED) or _is_full_exit_reason(reason):
        return available_amount

    t_sell_prefixes = (
        "触发短线止盈",
        "盘中冲高回落",
        "盘中收弱",
        "回落成本线保护",
    )
    if reason.startswith(t_sell_prefixes):
        full_exit_amount = _number_or(params.get("short_full_exit_profit_max_amount"), settings.PAPER_AUTO_SHORT_FULL_EXIT_PROFIT_MAX_AMOUNT)
        if full_exit_amount > 0 and available_amount <= full_exit_amount:
            return available_amount
        return max(100, _round_lot(available_amount * _number_or(params.get("t_sell_pct"), settings.PAPER_AUTO_T_SELL_PCT)))

    return max(100, _round_lot(available_amount * _number_or(params.get("t_weak_sell_pct"), settings.PAPER_AUTO_T_WEAK_SELL_PCT)))


async def _today_trade_amount(
    db: AsyncSession,
    account_id: int,
    code: str,
    trade_type: str,
    trade_date: date,
    since: Optional[datetime] = None,
    *,
    as_of: Optional[datetime] = None,
) -> int:
    start = since or datetime.combine(trade_date, time.min)
    end = datetime.combine(trade_date, time.max)
    if as_of is not None:
        end = min(end, as_of)
    amount = (
        await db.execute(
            select(func.coalesce(func.sum(PaperTradeLog.amount), 0)).where(
                PaperTradeLog.account_id == account_id,
                PaperTradeLog.code == code,
                PaperTradeLog.trade_type == trade_type,
                PaperTradeLog.trade_time >= start,
                PaperTradeLog.trade_time <= end,
            )
        )
    ).scalar_one()
    return int(amount or 0)


async def _available_sell_amount(
    db: AsyncSession,
    position: PaperPosition,
    trade_date: date,
) -> int:
    bought_today = await _today_trade_amount(db, position.account_id, position.code, "buy", trade_date)
    return _round_lot(int(position.buy_amount or 0) - bought_today)


async def _today_sell_stats(
    db: AsyncSession,
    account_id: int,
    code: str,
    trade_date: date,
    *,
    as_of: Optional[datetime] = None,
) -> dict:
    start = datetime.combine(trade_date, time.min)
    end = datetime.combine(trade_date, time.max)
    if as_of is not None:
        end = min(end, as_of)
    rows = (
        await db.execute(
            select(PaperTradeLog)
            .where(
                PaperTradeLog.account_id == account_id,
                PaperTradeLog.code == code,
                PaperTradeLog.trade_type == "sell",
                PaperTradeLog.trade_time >= start,
                PaperTradeLog.trade_time <= end,
            )
            .order_by(PaperTradeLog.trade_time)
        )
    ).scalars().all()
    if not rows:
        return {"amount": 0, "avg_price": None, "first_time": None}

    amount = sum(int(row.amount or 0) for row in rows)
    value = sum(float(row.price or 0) * int(row.amount or 0) for row in rows)
    first_time = rows[0].trade_time
    bought_after_sell = await _today_trade_amount(
        db, account_id, code, "buy", trade_date, first_time, as_of=as_of)
    return {
        "amount": max(0, amount - bought_after_sell),
        "avg_price": round(value / amount, 4) if amount else None,
        "first_time": first_time,
    }


async def _today_t_buyback_count(
    db: AsyncSession,
    account_id: int,
    code: str,
    trade_date: date,
    *,
    as_of: Optional[datetime] = None,
) -> int:
    start = datetime.combine(trade_date, time.min)
    end = datetime.combine(trade_date, time.max)
    if as_of is not None:
        end = min(end, as_of)
    return int((await db.execute(
        select(func.count(PaperAutoTradeLog.id)).where(
            PaperAutoTradeLog.account_id == account_id,
            PaperAutoTradeLog.trade_date == trade_date,
            PaperAutoTradeLog.source == "position-t",
            PaperAutoTradeLog.action == "buy",
            PaperAutoTradeLog.decision == "executed",
            PaperAutoTradeLog.code == code,
            PaperAutoTradeLog.created_at >= start,
            PaperAutoTradeLog.created_at <= end,
        )
    )).scalar_one() or 0)


async def _today_auto_new_buy_logs(
    db: AsyncSession,
    trade_date: date,
    account_id: Optional[int] = None,
    *,
    include_legacy_null: bool = False,
) -> list[PaperAutoTradeLog]:
    stmt = select(PaperAutoTradeLog).where(
        PaperAutoTradeLog.trade_date == trade_date,
        PaperAutoTradeLog.action == "buy",
        PaperAutoTradeLog.decision == "executed",
        PaperAutoTradeLog.source != "position-t",
    )
    if account_id is not None:
        if include_legacy_null:
            stmt = stmt.where(
                or_(PaperAutoTradeLog.account_id.is_(None), PaperAutoTradeLog.account_id == account_id)
            )
        else:
            stmt = stmt.where(PaperAutoTradeLog.account_id == account_id)
    rows = (
        await db.execute(
            stmt.order_by(PaperAutoTradeLog.created_at, PaperAutoTradeLog.id)
        )
    ).scalars().all()
    return list(rows)


def _sector_counts_from_auto_logs(logs: list[PaperAutoTradeLog]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for log in logs:
        candidate = _json_loads_dict(log.candidate_json)
        sector = _candidate_sector_key(candidate)
        if not sector:
            continue
        counts[sector] = counts.get(sector, 0) + 1
    return counts


def _green_reversal_leader_first_move_reason(
    *,
    change_pct: Optional[float],
    sector_change_pct: float,
    volume_ratio: Optional[float],
    orderbook_imbalance: Optional[float],
) -> str:
    if not settings.PAPER_AUTO_GREEN_REVERSAL_LEADER_ENABLED:
        return ""
    if change_pct is None:
        return ""
    sector_move = max(0.0, sector_change_pct)
    premium = change_pct - sector_move
    if change_pct < settings.PAPER_AUTO_GREEN_REVERSAL_LEADER_MIN_CHANGE_PCT:
        return ""
    if premium < settings.PAPER_AUTO_GREEN_REVERSAL_LEADER_MIN_PREMIUM_PCT:
        return ""
    if volume_ratio is None or volume_ratio < settings.PAPER_AUTO_GREEN_REVERSAL_LEADER_MIN_VOLUME_RATIO:
        return ""
    if (
        orderbook_imbalance is None
        or orderbook_imbalance < settings.PAPER_AUTO_GREEN_REVERSAL_LEADER_MIN_ORDERBOOK
    ):
        return ""
    return (
        f"个股涨幅{change_pct:.2f}%领先板块{sector_change_pct:.2f}%"
        f"{premium:.2f}个百分点，量比{volume_ratio:.2f}，盘口{orderbook_imbalance:.2f}"
    )


def _is_reversal_buy_source(source: str) -> bool:
    return source in {"green_limit_reversal", "underwater_reversal"}


def _is_generic_or_cold_sector_name(name: str | None) -> bool:
    value = str(name or "").strip()
    if not value:
        return True
    return any(keyword in value for keyword in _PAPER_GENERIC_SECTOR_KEYWORDS)


def _default_stop_loss_price(price: float, stop_loss_pct: Optional[float] = None) -> float:
    pct = stop_loss_pct if stop_loss_pct is not None else settings.PAPER_AUTO_STOP_LOSS_PCT
    return round(float(price or 0) * (1 - abs(float(pct or 0)) / 100), 2) if price else 0.0


def _plan_strategy_types(plan: dict) -> set[str]:
    return {
        str(strategy.get("strategy_type") or "").strip()
        for strategy in (plan.get("strategies") or [])
        if str(strategy.get("strategy_type") or "").strip()
    }


def _plan_primary_strategy(plan: dict) -> dict:
    strategies = [item for item in (plan.get("strategies") or []) if isinstance(item, dict)]
    for strategy in strategies:
        if str(strategy.get("strategy_type") or "") in _PAPER_ACTIONABLE_PLAN_TYPES:
            return strategy
    return strategies[0] if strategies else {}


def _plan_is_direct_buy_candidate(plan: dict) -> bool:
    if plan.get("is_tradeable") is False:
        return False
    strategy_types = _plan_strategy_types(plan)
    if not strategy_types.intersection(_PAPER_ACTIONABLE_PLAN_TYPES):
        return False
    try:
        from app.api.v1.tenbagger import _next_day_plan_action_priority

        return _next_day_plan_action_priority(plan) <= 3
    except Exception:
        action_text = " ".join([
            str(plan.get("action") or ""),
            str(plan.get("invalidation") or ""),
            " ".join(str(item or "") for item in plan.get("avoid_reasons") or []),
            " ".join(str(item or "") for item in plan.get("risk_warnings") or []),
        ])
        return "观察" not in action_text and "先观察" not in action_text and "不建议" not in action_text


def _plan_hot_sector_gate(plan: dict) -> tuple[bool, str, dict]:
    name = str(plan.get("sector_resonance_name") or "").strip()
    resonance = str(plan.get("sector_resonance") or "").strip()
    score = _to_float(plan.get("sector_resonance_score")) or 0.0
    change_pct = _to_float(plan.get("sector_resonance_change")) or 0.0
    fund_flow = _to_float(plan.get("sector_resonance_fund")) or 0.0
    if _is_generic_or_cold_sector_name(name):
        return False, "缺少有效题材主线或仅命中泛概念/交易属性板块", {}

    hot = (
        resonance == "强共振" and score >= 60 and (change_pct >= -0.3 or fund_flow > 0)
    ) or (
        score >= 70 and change_pct >= -0.5 and fund_flow >= 0
    ) or (
        fund_flow >= 3 and change_pct >= 0 and score >= 50
    )
    if not hot:
        return (
            False,
            f"{name}题材合力不足：{resonance or '无共振'}，强度{score:.0f}，涨幅{change_pct:.2f}%，资金{fund_flow:.2f}亿",
            {
                "sector_name": name,
                "sector_resonance": resonance,
                "sector_strength": score,
                "sector_change_pct": change_pct,
                "sector_fund_flow": fund_flow,
            },
        )
    return (
        True,
        f"{name}{resonance or '主线'}：强度{score:.0f}，涨幅{change_pct:.2f}%，资金{fund_flow:.2f}亿",
        {
            "sector_name": name,
            "sector_resonance": resonance,
            "sector_strength": score,
            "sector_change_pct": change_pct,
            "sector_fund_flow": fund_flow,
        },
    )


def _active_sector_from_anomaly_detail(detail: dict) -> tuple[dict, str]:
    from app.signal.anomaly_scanner import _is_causal_trade_driver_sector

    candidates: list[dict] = []
    for raw_sector in detail.get("sector_factors") or []:
        sector = dict(raw_sector or {})
        name = str(sector.get("sector_name") or "").strip()
        if (
            _is_generic_or_cold_sector_name(name)
            or not _is_causal_trade_driver_sector(sector)
        ):
            continue
        strength = _to_float(sector.get("strength_score")) or 0.0
        change_pct = _to_float(sector.get("change_pct")) or 0.0
        fund_flow = _to_float(sector.get("fund_flow")) or 0.0
        limit_up_count = int(sector.get("limit_up_count") or 0)
        hot = (
            fund_flow > 0 and change_pct > 0 and strength >= 50
        ) or (
            limit_up_count > 0 and strength >= 55 and change_pct >= -0.5
        )
        if hot:
            candidates.append(sector)
    if not candidates:
        return {}, "未匹配到资金/涨幅/强度同步为正的主线板块，避免买入冷门板块"
    candidates.sort(
        key=lambda sector: (
            bool(sector.get("is_sector_leader")),
            _to_float(sector.get("lifecycle_score")) or 0.0,
            _to_float(sector.get("strength_score")) or 0.0,
            int(sector.get("limit_up_count") or 0),
            _to_float(sector.get("fund_flow")) or 0.0,
            _to_float(sector.get("change_pct")) or 0.0,
        ),
        reverse=True,
    )
    sector = candidates[0]
    name = str(sector.get("sector_name") or "").strip()
    strength = _to_float(sector.get("strength_score")) or 0.0
    change_pct = _to_float(sector.get("change_pct")) or 0.0
    fund_flow = _to_float(sector.get("fund_flow")) or 0.0
    limit_up_count = int(sector.get("limit_up_count") or 0)
    return sector, f"{name}主线活跃：强度{strength:.0f}，涨幅{change_pct:.2f}%，资金{fund_flow:.2f}亿，涨停{limit_up_count}家"


async def _is_signal_snapshot_valid_for_trade(snapshot_trade_date: Optional[date], trade_date: date) -> bool:
    if snapshot_trade_date is None:
        return False
    if snapshot_trade_date == trade_date:
        return True
    return await trade_calendar.next_trade_day(snapshot_trade_date) == trade_date


def _candidate_stop_loss(candidate: dict, price: float) -> float:
    raw = _to_float(candidate.get("stop_loss_price"))
    if raw and price > 0 and raw < price:
        return round(raw, 2)
    stop_pct = _to_float(candidate.get("stop_loss_pct"))
    if stop_pct:
        return _default_stop_loss_price(price, abs(stop_pct))
    return _default_stop_loss_price(price)


def _candidate_is_observation_only(candidate: dict) -> bool:
    """候选池身份不能代替盘中买点确认。"""
    source = str(candidate.get("_source") or "")
    if source in {"radar", "candidate", ""}:
        return True
    if source in {
        "daily_participation",
        "ma5_pullback",
        "green_limit_reversal",
        "underwater_reversal",
        "icepoint_reversal",
    } and not candidate.get("execution_confirmation"):
        return True
    if candidate.get("execution_confirmation"):
        return False
    texts = [candidate.get("suggestion"), candidate.get("action")]
    for key in ("risk_warnings", "avoid_reasons"):
        value = candidate.get(key)
        if isinstance(value, (list, tuple, set)):
            texts.extend(value)
        elif value:
            texts.append(value)
    joined = " ".join(str(item or "") for item in texts)
    return any(
        marker in joined
        for marker in ("观望为主", "只入检测池", "不建议", "禁止追", "等待盘口确认")
    )


def _candidate_allows_continuous_participation(candidate: dict, score: float) -> bool:
    """深回撤期只给已确认的低吸/回收信号分层建仓权，不给追涨异动穿透风控。"""
    if not settings.PAPER_AUTO_DRAWDOWN_CONTINUOUS_PARTICIPATION_ENABLED:
        return False
    if score < settings.PAPER_AUTO_DRAWDOWN_CONTINUOUS_PARTICIPATION_MIN_SCORE:
        return False
    if not candidate.get("execution_confirmation"):
        return False
    return str(candidate.get("_source") or "") in {
        "daily_participation",
        "ma5_pullback",
        "underwater_reversal",
        "green_limit_reversal",
        "icepoint_reversal",
    }


def _candidate_price_levels(candidate: dict, key: str) -> list[float]:
    values: list[float] = []
    containers = [
        candidate,
        candidate.get("main_wave_stats") or {},
        candidate.get("dynamic_pool_stats") or {},
        candidate.get("strategy") or {},
    ]
    aliases = {
        "support": ("support", "support_price", "ma5", "ma10"),
        "resistance": ("resistance", "pressure", "target_price", "target"),
    }
    for container in containers:
        if not isinstance(container, dict):
            continue
        for alias in aliases.get(key, (key,)):
            value = _to_float(container.get(alias))
            if value is not None and value > 0:
                values.append(value)
    return values


def _a_entry_price_band(spot: StockSpot | None) -> dict:
    """只审计A正涨幅入口的必要条件交集，不生成信号或放宽任一原阈值。

    分时稳定要求报价 >= H*(1-回撤上限)，低吸要求成交价 <= L*(1+反弹上限)。
    买单还须经过盘口/其余策略/资金风控；交集非空绝不等于可买。
    """
    high = _to_float(getattr(spot, "high", None))
    low = _to_float(getattr(spot, "low", None))
    change = _to_float(getattr(spot, "change_pct", None))
    if high is None or low is None or low <= 0 or high <= low or change is None or change <= 0:
        return {"status": "not_applicable", "sufficient_for_entry": False}
    drawdown = max(0.0, float(account_confirmation_policy()["max_pullback_from_high_pct"]))
    rebound = float(settings.PAPER_AUTO_VALUE_ENTRY_MAX_REBOUND_FROM_LOW_PCT)
    lower = high * (1 - drawdown / 100)
    upper = low * (1 + rebound / 100)
    return {
        "status": "empty" if lower > upper + 1e-8 else "nonempty",
        "lower_price": round(lower, 8),
        "upper_price": round(upper, 8),
        "high_price": high, "low_price": low,
        "max_high_drawdown_pct": drawdown,
        "max_low_rebound_pct": rebound,
        "sufficient_for_entry": False,
        "basis": "current_round_necessary_constraints",
    }


def _candidate_execution_value_reject_reason(
    candidate: dict,
    *,
    price: float,
    spot: StockSpot | None,
) -> str:
    """自动下单的最后一道价格闸门；只使用下单时已经可见的数据。"""
    if _candidate_is_observation_only(candidate):
        return "候选仍属于观察池/等待确认，不能把形态评分直接升级为自动买单"
    if price <= 0:
        return "缺少有效成交价"

    change_pct = _to_float(getattr(spot, "change_pct", None))
    if change_pct is None:
        change_pct = _to_float(candidate.get("change_pct"))
    high = _to_float(getattr(spot, "high", None))
    if high is None:
        high = _to_float(candidate.get("high_price")) or _to_float(candidate.get("high"))
    low = _to_float(getattr(spot, "low", None))
    if low is None:
        low = _to_float(candidate.get("low_price")) or _to_float(candidate.get("low"))
    avg_price = _to_float(getattr(spot, "avg_price", None))
    if avg_price is None:
        avg_price = _to_float(candidate.get("avg_price"))

    if (
        change_pct is not None
        and change_pct > settings.PAPER_AUTO_VALUE_ENTRY_MAX_CHANGE_PCT
    ):
        return (
            f"当前涨幅{change_pct:.2f}%超过自动低吸上限"
            f"{settings.PAPER_AUTO_VALUE_ENTRY_MAX_CHANGE_PCT:.2f}%，异动仅观察不追价"
        )
    if high and high > 0 and change_pct is not None:
        high_gap_pct = max(0.0, (high - price) / high * 100)
        if (
            change_pct >= settings.PAPER_AUTO_VALUE_ENTRY_HIGH_GAP_MIN_CHANGE_PCT
            and high_gap_pct <= settings.PAPER_AUTO_VALUE_ENTRY_MAX_HIGH_GAP_PCT
        ):
            return (
                f"现价距日内高点仅{high_gap_pct:.2f}%且已涨{change_pct:.2f}%，"
                "等待首次回踩或支撑回收，不在脉冲高点下单"
            )
    if avg_price and avg_price > 0 and change_pct is not None and change_pct > 0:
        avg_premium_pct = (price / avg_price - 1) * 100
        if avg_premium_pct > settings.PAPER_AUTO_VALUE_ENTRY_MAX_VWAP_PREMIUM_PCT:
            return (
                f"现价高于VWAP {avg_premium_pct:.2f}%，超过性价比上限"
                f"{settings.PAPER_AUTO_VALUE_ENTRY_MAX_VWAP_PREMIUM_PCT:.2f}%"
            )
    if high and low and high > low:
        range_position = (price - low) / (high - low)
        rebound_from_low_pct = (price / low - 1) * 100
        if (
            change_pct is not None
            and change_pct > 0
            and range_position > settings.PAPER_AUTO_VALUE_ENTRY_MAX_RANGE_POSITION
        ):
            return (
                f"现价位于日内振幅{range_position * 100:.1f}%分位，超过"
                f"{settings.PAPER_AUTO_VALUE_ENTRY_MAX_RANGE_POSITION * 100:.0f}%上限，等待回踩"
            )
        if (
            change_pct is not None
            and change_pct > 0
            and rebound_from_low_pct > settings.PAPER_AUTO_VALUE_ENTRY_MAX_REBOUND_FROM_LOW_PCT
        ):
            return (
                f"现价已从日内低点反弹{rebound_from_low_pct:.2f}%，超过自动低吸上限"
                f"{settings.PAPER_AUTO_VALUE_ENTRY_MAX_REBOUND_FROM_LOW_PCT:.2f}%"
            )

    supports = [value for value in _candidate_price_levels(candidate, "support") if value < price]
    if supports:
        nearest_support = max(supports)
        support_gap_pct = (price / nearest_support - 1) * 100
        if support_gap_pct > settings.PAPER_AUTO_VALUE_ENTRY_MAX_SUPPORT_GAP_PCT:
            return (
                f"现价距最近支撑{nearest_support:.2f}为{support_gap_pct:.2f}%，"
                "止损空间过大，等待回踩"
            )

    resistances = _candidate_price_levels(candidate, "resistance")
    if resistances:
        higher_resistances = [value for value in resistances if value > price]
        if not higher_resistances:
            return "现价已越过候选压力位且未完成回踩确认，禁止在突破脉冲末端追单"
        stop_loss = _candidate_stop_loss(candidate, price)
        risk = price - stop_loss if 0 < stop_loss < price else 0.0
        reward = min(higher_resistances) - price
        if risk > 0 and reward / risk < settings.PAPER_AUTO_VALUE_ENTRY_MIN_REWARD_RISK:
            return (
                f"按最近压力位计算盈亏比仅{reward / risk:.2f}，低于"
                f"{settings.PAPER_AUTO_VALUE_ENTRY_MIN_REWARD_RISK:.2f}"
            )
    return ""


def _paper_effective_min5_change(
    spot: StockSpot | None,
    *,
    trade_date: date,
) -> Optional[float]:
    """返回可用于交易确认的5分钟涨跌，实时窗口未形成时保持None。"""
    if spot is None:
        return None
    stored = _to_float(getattr(spot, "min5_change", None))
    if trade_date != _paper_now().date():
        return stored if stored is not None and abs(stored) <= 10.0 else None

    # 腾讯源不提供可信5分钟涨跌；实时交易只能消费连续轮次形成的内存窗口。
    # None表示尚无4分30秒至5分30秒基线，必须继续观察，不能伪造0%。
    try:
        from app.signal.anomaly_scanner import anomaly_scanner

        observed = anomaly_scanner.track_intraday_min5_change(spot)
    except Exception as exc:
        logger.debug(f"模拟盘5分钟动能计算失败，保守等待: {exc}")
        return None
    return _to_float(observed)


def _reversal_retest_confirmed(
    *,
    change_pct: Optional[float],
    avg_premium_pct: Optional[float],
    close_position: Optional[float],
    min5_change: Optional[float],
) -> bool:
    """急拉后的首次停顿确认；缺任一实时位置数据时只观察，不猜测成交。"""
    if None in (change_pct, avg_premium_pct, close_position, min5_change):
        return False
    return (
        0 <= float(change_pct) <= settings.PAPER_AUTO_REVERSAL_RETEST_MAX_CHANGE_PCT
        and float(avg_premium_pct) <= settings.PAPER_AUTO_REVERSAL_RETEST_MAX_VWAP_PREMIUM_PCT
        and float(close_position) <= settings.PAPER_AUTO_REVERSAL_RETEST_MAX_RANGE_POSITION
        and settings.PAPER_AUTO_REVERSAL_RETEST_MIN_5M_CHANGE_PCT
        <= float(min5_change)
        <= settings.PAPER_AUTO_REVERSAL_RETEST_MAX_5M_CHANGE_PCT
    )


def _candidate_buy_reason(candidate: dict, score: float, stop_loss: float) -> str:
    source = str(candidate.get("_source") or "")
    sector_reason = str(candidate.get("sector_reason") or "").strip()
    if source == "next_day_plan":
        parts = [
            f"高胜率明日预案：{candidate.get('strategy_label') or candidate.get('candidate_source_label') or '直接买点'}",
            str(candidate.get("entry_condition") or "").strip(),
            sector_reason,
            f"评分{score:.1f}",
            f"止损{stop_loss:.2f}" if stop_loss else "",
        ]
    elif source == "anomaly_buy_point":
        reasons = "；".join(str(item) for item in (candidate.get("buy_point_reasons") or []) if item)
        parts = [
            f"异动到达买点：{candidate.get('buy_point_type') or '买点确认'}",
            reasons,
            sector_reason,
            f"评分{score:.1f}",
            f"止损{stop_loss:.2f}" if stop_loss else "",
        ]
    elif source in {"green_limit_reversal", "underwater_reversal"}:
        title = "水下翻红：水下承接后拉回红盘" if source == "underwater_reversal" else "低吸弱转强：低开承接后回到水上"
        parts = [
            title,
            str(candidate.get("leader_first_move_reason") or "").strip(),
            sector_reason,
            f"开盘{_to_float(candidate.get('open_change_pct')) or 0:.2f}%",
            f"最低{_to_float(candidate.get('low_drop_pct')) or 0:.2f}%" if source == "underwater_reversal" else "",
            f"当前{_to_float(candidate.get('change_pct')) or 0:.2f}%",
            f"评分{score:.1f}",
            f"止损{stop_loss:.2f}" if stop_loss else "",
        ]
    elif source == "ma5_pullback":
        parts = [
            "上升通道回踩5日线",
            f"现价/MA5偏离{_to_float(candidate.get('ma5_distance_pct')) or 0:.2f}%",
            f"MA5 {(_to_float(candidate.get('ma5')) or 0):.2f}",
            f"MA10 {(_to_float(candidate.get('ma10')) or 0):.2f}",
            f"评分{score:.1f}",
            f"止损{stop_loss:.2f}" if stop_loss else "",
        ]
    elif source == "icepoint_reversal":
        parts = [
            "冰点弱修复低吸：先下探、放量回收并站稳VWAP附近",
            str(candidate.get("sector_reason") or "").strip(),
            f"日内低点反弹{_to_float(candidate.get('rebound_from_low_pct')) or 0:.2f}%",
            f"评分{score:.1f}",
            f"止损{stop_loss:.2f}" if stop_loss else "",
        ]
    elif source == "daily_participation":
        confirmations = "、".join(
            str(item) for item in (candidate.get("intraday_confirmations") or []) if item
        )
        parts = [
            "盘面滚动观察仓：低热度趋势回收确认",
            confirmations,
            f"低点反弹{_to_float(candidate.get('rebound_from_low_pct')) or 0:.2f}%",
            f"VWAP偏离{_to_float(candidate.get('avg_premium_pct')) or 0:.2f}%",
            f"量比{_to_float(candidate.get('volume_ratio')) or 0:.2f}",
            sector_reason,
            f"评分{score:.1f}",
            f"止损{stop_loss:.2f}" if stop_loss else "",
        ]
    else:
        parts = [f"自动买点，评分{score:.1f}", sector_reason]
    return " | ".join(part for part in parts if part)


def _short_weak_confirmation_count(
    *,
    price: Optional[float],
    open_price: Optional[float],
    avg_price: Optional[float],
    ma5: Optional[float],
    orderbook_imbalance: Optional[float],
    volume_ratio: Optional[float],
    change_pct: Optional[float],
) -> int:
    confirmations = 0
    if price is not None and avg_price is not None and price < avg_price:
        confirmations += 1
    if price is not None and open_price is not None and price < open_price:
        confirmations += 1
    if ma5 is not None and price is not None and price < ma5:
        confirmations += 1
    if orderbook_imbalance is not None and orderbook_imbalance <= -0.35:
        confirmations += 1
    if (
        volume_ratio is not None
        and volume_ratio >= settings.PAPER_AUTO_VOLUME_NEGATIVE_RATIO
        and change_pct is not None
        and change_pct < 0
    ):
        confirmations += 1
    return confirmations


def _short_independent_weak_evidence_count(
    *,
    price: Optional[float],
    open_price: Optional[float],
    avg_price: Optional[float],
    ma5: Optional[float],
    min5_change: Optional[float],
    orderbook_imbalance: Optional[float],
    volume_ratio: Optional[float],
    change_pct: Optional[float],
) -> int:
    """开盘噪声窗专用：只统计"独立走弱证据"。

    2026-09-17 复盘缺陷：`_short_weak_confirmation_count` 的 5 项里，
    「现价<开盘」与「现价<均价」在止损价被击穿时必然成立（止损价低于成本，
    价格跌到止损位必然同时低于当日开盘与分时均价），因此这两项对
    "噪声回踩 vs 真实走弱"没有区分度。实测历史 61 次窗内止损触发中，
    该计数 <2 的为 0 次 —— 即开盘噪声豁免分支从未生效，
    `PAPER_AUTO_OPEN_SEVERE_STOP_LOSS_PCT` 形同虚设。

    这里改为只剔除确证同义反复的两项，保留其余有区分度的信号。
    实测（历史 61 次窗内止损触发）各项命中率：
        现价<开盘 62%、现价<均价 62%   -> 止损点必然成立，剔除
        现价<MA5  35%                  -> 有区分度，保留
        五档卖压 18%、放量下跌 67%      -> 保留
    统计证据：剔除两项后，窗内止损的独立证据数分布为
    {1:9, 2:33, 3:17, 4:2}（下界 1）。若门槛取 1 则豁免仍不会生效；
    门槛取 2 时豁免 15% 的窗内止损，是保守且可解释的取值。

    与 `_short_weak_confirmation_count` 并存：后者仍用于
    `_confirmed_sector_retreat_sell_reason` 的板块退潮佐证，语义不变。
    """
    evidences = 0
    if ma5 is not None and price is not None and price < ma5:
        evidences += 1
    if orderbook_imbalance is not None and orderbook_imbalance <= -0.35:
        evidences += 1
    if (
        volume_ratio is not None
        and volume_ratio >= settings.PAPER_AUTO_VOLUME_NEGATIVE_RATIO
        and change_pct is not None
        and change_pct < 0
    ):
        evidences += 1
    if min5_change is not None and min5_change <= -0.5:
        evidences += 1
    return evidences


def _confirmed_sector_retreat_sell_reason(
    sector_retreat_reason: str,
    *,
    price: Optional[float],
    open_price: Optional[float],
    avg_price: Optional[float],
    ma5: Optional[float],
    min5_change: Optional[float],
    orderbook_imbalance: Optional[float],
    volume_ratio: Optional[float],
    change_pct: Optional[float],
    close_position: Optional[float],
) -> str:
    """板块退潮只能加速已被个股盘口确认的退出，不能单独触发清仓。"""
    if not sector_retreat_reason:
        return ""
    confirmations = _short_weak_confirmation_count(
        price=price,
        open_price=open_price,
        avg_price=avg_price,
        ma5=ma5,
        orderbook_imbalance=orderbook_imbalance,
        volume_ratio=volume_ratio,
        change_pct=change_pct,
    )
    weak_parts: list[str] = []
    if price is not None and avg_price is not None and price < avg_price:
        weak_parts.append("跌破分时均价")
    if price is not None and open_price is not None and price < open_price:
        weak_parts.append("跌破开盘价")
    if price is not None and ma5 is not None and price < ma5:
        weak_parts.append("跌破5日线")
    if orderbook_imbalance is not None and orderbook_imbalance <= -0.35:
        weak_parts.append("盘口卖压")
    if (
        volume_ratio is not None
        and volume_ratio >= settings.PAPER_AUTO_VOLUME_NEGATIVE_RATIO
        and change_pct is not None
        and change_pct < 0
    ):
        weak_parts.append("放量下跌")
    if min5_change is not None and min5_change <= -1.0:
        confirmations += 1
        weak_parts.append("5分钟急跌")
    if close_position is not None and close_position < 0.35:
        confirmations += 1
        weak_parts.append("日内位置偏低")
    if confirmations < 2:
        return ""
    details = "、".join(dict.fromkeys(weak_parts)) or "至少两项个股弱势信号"
    return f"{sector_retreat_reason}；个股同步转弱确认：{details}"


def _anomaly_sector_reject_reason(strength: float, sector_change_pct: float) -> str:
    if strength < settings.PAPER_AUTO_ANOMALY_MIN_SECTOR_STRENGTH:
        return (
            f"板块强度{strength:.1f}<"
            f"{settings.PAPER_AUTO_ANOMALY_MIN_SECTOR_STRENGTH:.1f}"
        )
    if (
        sector_change_pct < settings.PAPER_AUTO_ANOMALY_MIN_SECTOR_CHANGE_PCT
        and strength < settings.PAPER_AUTO_ANOMALY_STRONG_SECTOR_OVERRIDE
    ):
        return (
            f"板块涨幅{sector_change_pct:.2f}%<"
            f"{settings.PAPER_AUTO_ANOMALY_MIN_SECTOR_CHANGE_PCT:.2f}%"
            f"且强度{strength:.1f}<强势豁免线"
            f"{settings.PAPER_AUTO_ANOMALY_STRONG_SECTOR_OVERRIDE:.1f}"
        )
    return ""


def _anomaly_position_score(raw_score: float, strength: float, sector_change_pct: float) -> float:
    score = min(99.0, raw_score)
    if (
        strength < settings.PAPER_AUTO_ANOMALY_STRONG_SECTOR_OVERRIDE
        or sector_change_pct < settings.PAPER_AUTO_ANOMALY_MIN_SECTOR_CHANGE_PCT + 0.5
    ):
        return min(score, settings.PAPER_AUTO_ANOMALY_EDGE_SCORE_CAP)
    return score


async def _apply_auto_position_risk(
    db: AsyncSession,
    account_id: int,
    code: str,
    *,
    reason: str,
    stop_loss_price: float,
) -> None:
    position = (
        await db.execute(
            select(PaperPosition)
            .where(
                PaperPosition.account_id == account_id,
                PaperPosition.code == code,
                PaperPosition.is_closed.is_(False),
            )
            .order_by(desc(PaperPosition.buy_time))
            .limit(1)
        )
    ).scalar_one_or_none()
    if not position:
        return
    if stop_loss_price and stop_loss_price > 0:
        position.stop_loss_price = round(stop_loss_price, 2)
    position.buy_reason = reason
    await db.flush()


async def _next_day_plan_buy_candidates(
    db: AsyncSession,
    *,
    limit: int,
    trade_date: date,
) -> tuple[list[dict], list[str]]:
    notes: list[str] = []
    try:
        from app.api.v1.tenbagger import prewarm_next_day_plan_snapshot

        snapshot = await prewarm_next_day_plan_snapshot(db, None, force_refresh=False, limit=max(limit, 20))
    except Exception as exc:
        await db.rollback()
        return [], [f"高胜率明日预案加载失败：{exc}"]

    snapshot_date = None
    try:
        snapshot_date = date.fromisoformat(str(snapshot.get("trade_date") or ""))
    except ValueError:
        snapshot_date = None
    if not await _is_signal_snapshot_valid_for_trade(snapshot_date, trade_date):
        return [], [f"明日预案最新信号日为{snapshot.get('trade_date') or '--'}，不是今日可执行预案"]

    candidates: list[dict] = []
    rejected = 0
    for plan in snapshot.get("plans") or []:
        if not _plan_is_direct_buy_candidate(plan):
            continue
        sector_ok, sector_reason, sector_ctx = _plan_hot_sector_gate(plan)
        if not sector_ok:
            rejected += 1
            continue
        strategy = _plan_primary_strategy(plan)
        price = _to_float(plan.get("price")) or 0.0
        stop_loss = _to_float(strategy.get("stop_loss")) or _to_float(plan.get("support"))
        if stop_loss and stop_loss >= price:
            stop_loss = 0.0
        score = max(_to_float(plan.get("bull_score")) or 0.0, _to_float(plan.get("main_wave_score")) or 0.0)
        candidate = {
            **plan,
            **sector_ctx,
            "_source": "next_day_plan",
            "signal_source": _PAPER_BUY_SOURCE_LABELS["next_day_plan"],
            "total_score": min(99.0, score + 3.0),
            "price": price,
            "strategy_label": strategy.get("strategy_label") or plan.get("action") or "",
            "entry_condition": strategy.get("entry_condition") or "",
            "candidate_source_label": plan.get("candidate_source_label") or "",
            "stop_loss_price": round(stop_loss, 2) if stop_loss else _default_stop_loss_price(price),
            "stop_loss_pct": strategy.get("stop_loss_pct"),
            "sector_reason": sector_reason,
        }
        candidates.append(candidate)

    if rejected:
        notes.append(f"已过滤{rejected}只非主线/冷门板块预案候选")
    candidates.sort(
        key=lambda item: (
            _to_float(item.get("total_score")) or 0,
            _to_float(item.get("sector_strength")) or 0,
            _to_float(item.get("sector_fund_flow")) or 0,
        ),
        reverse=True,
    )
    return candidates[:limit], notes


async def _ranked_sector_contexts_for_code(
    db: AsyncSession,
    code: str,
    trade_date: Optional[date] = None,
) -> list[dict]:
    """返回个股最强有效板块；传入交易日时只使用当日板块快照。

    盘中买卖决策不得把数月前的高强度记录当作当前板块状态。未传日期仅供
    历史展示/兼容调用读取各板块最新记录，交易链路必须显式传入 trade_date。
    """
    from app.signal.anomaly_scanner import _is_causal_trade_driver_sector

    mappings = list((await db.execute(
        select(StockSectorMapping).where(
            StockSectorMapping.code == code,
            StockSectorMapping.sector_type.in_(("concept", "industry")),
        )
    )).scalars().all())
    semantic_mappings = [
        mapping
        for mapping in mappings
        if not _is_generic_or_cold_sector_name(mapping.sector_name or mapping.sector_code)
    ]
    preferred = [
        mapping
        for mapping in semantic_mappings
        if _is_causal_trade_driver_sector({
            "sector_name": mapping.sector_name or mapping.sector_code,
            "sector_type": mapping.sector_type,
            "source": mapping.source,
        })
    ]
    effective_mappings = preferred or semantic_mappings
    if not effective_mappings:
        return []

    sector_codes = {str(mapping.sector_code or "") for mapping in effective_mappings if mapping.sector_code}
    persistence_stmt = select(SectorPersistence).where(
        SectorPersistence.sector_code.in_(sector_codes)
    )
    if trade_date is not None:
        persistence_stmt = persistence_stmt.where(
            SectorPersistence.trade_date == trade_date
        )
    persistence_rows = list((await db.execute(
        persistence_stmt.order_by(
            desc(SectorPersistence.trade_date),
            desc(SectorPersistence.id),
        )
    )).scalars().all())
    latest_by_code: dict[str, SectorPersistence] = {}
    for row in persistence_rows:
        latest_by_code.setdefault(str(row.sector_code or ""), row)

    contexts: list[dict] = []
    for mapping in effective_mappings:
        sector_code = str(mapping.sector_code or "")
        latest = latest_by_code.get(sector_code)
        sector_name = str(
            getattr(latest, "sector_name", None)
            or mapping.sector_name
            or sector_code
            or ""
        )
        contexts.append({
            "sector_code": sector_code,
            "sector_name": sector_name,
            "sector_strength": _to_float(getattr(latest, "strength_score", None)) or 0.0,
            "sector_change_pct": _to_float(getattr(latest, "change_pct", None)) or 0.0,
            "sector_fund_flow": _to_float(getattr(latest, "fund_flow", None)) or 0.0,
            "sector_limit_up_count": int(getattr(latest, "limit_up_count", 0) or 0),
            "sector_consecutive_days": int(getattr(latest, "consecutive_days", 0) or 0),
            "sector_source": str(mapping.source or ""),
            "sector_type": str(mapping.sector_type or ""),
            "sector_trade_date": (
                latest.trade_date.isoformat()
                if latest is not None and latest.trade_date is not None
                else None
            ),
            "_persistence": latest,
            "_mapping_name": str(mapping.sector_name or ""),
        })
    contexts.sort(
        key=lambda item: (
            item.get("_persistence") is not None,
            _to_float(item.get("sector_strength")) or 0.0,
            int(item.get("sector_limit_up_count") or 0),
            _to_float(item.get("sector_fund_flow")) or 0.0,
            _to_float(item.get("sector_change_pct")) or 0.0,
        ),
        reverse=True,
    )
    return contexts


async def _latest_sector_context_for_code(
    db: AsyncSession,
    code: str,
    trade_date: Optional[date] = None,
) -> tuple[dict, str]:
    contexts = await _ranked_sector_contexts_for_code(
        db,
        code,
        trade_date=trade_date,
    )
    if not contexts:
        return {}, ""
    selected = dict(contexts[0])
    latest = selected.pop("_persistence", None)
    selected.pop("_mapping_name", None)
    sector_name = selected.get("sector_name") or selected.get("sector_code") or ""
    if latest is None:
        return selected, f"{sector_name}板块数据缺失，以个股绿盘转强为主"
    return selected, (
        f"{sector_name}板块强度{selected['sector_strength']:.0f}，"
        f"涨幅{selected['sector_change_pct']:.2f}%，资金{selected['sector_fund_flow']:.2f}亿，"
        f"涨停{selected['sector_limit_up_count']}家，持续{selected['sector_consecutive_days']}日"
    )


async def _resolve_candidate_entry_sector(
    db: AsyncSession,
    candidate: dict,
    trade_date: Optional[date] = None,
) -> dict:
    code = str(candidate.get("code") or "").strip()
    if not code:
        return {}
    contexts = await _ranked_sector_contexts_for_code(
        db,
        code,
        trade_date=trade_date,
    )
    if not contexts:
        return {}
    requested_code = str(candidate.get("sector_code") or "").strip()
    requested_name = str(
        candidate.get("sector_name")
        or candidate.get("sector_resonance_name")
        or candidate.get("driver_primary")
        or ""
    ).strip()
    # 入场板块必须来自候选生成时的可审计证据。像晋级预测这种未声明板块
    # 论点的候选，不得在下单阶段从个股的多概念映射中猜一个“最强板块”。
    if not requested_code and not requested_name:
        return {}
    selected = next(
        (
            item for item in contexts
            if requested_code and str(item.get("sector_code") or "") == requested_code
        ),
        None,
    )
    if selected is None and requested_name:
        selected = next(
            (
                item for item in contexts
                if requested_name in {
                    str(item.get("sector_name") or ""),
                    str(item.get("_mapping_name") or ""),
                }
            ),
            None,
        )
    selected = selected or contexts[0]
    return {
        "entry_sector_code": str(selected.get("sector_code") or "") or None,
        "entry_sector_name": str(selected.get("sector_name") or "") or None,
    }


def _reversal_universe_sql_filters(trade_date: date) -> tuple:
    """资格先于排名截断；只复用现有板块标记，最终下单风控仍需再校验。"""
    prefixes = [
        prefix for prefix, board in CODE_PREFIX_MAP.items()
        if BOARD_TAG_MAP.get(board) == TAG_TRADEABLE
    ]
    invalid_tag = select(StockTag.code).where(
        StockTag.code == StockSpot.code,
        or_(StockTag.board_tag.is_(None), StockTag.board_tag != TAG_TRADEABLE,
            StockTag.is_st.is_(True), StockTag.is_suspended.is_(True),
            StockTag.is_delisting.is_(True)),
    ).exists()
    blacklisted = select(StockBlacklist.code).where(
        StockBlacklist.code == StockSpot.code,
        StockBlacklist.reason.in_(["st", "delisting", "suspended"]),
        _active_blacklist_clause(trade_date),
    ).exists()
    name = func.coalesce(StockSpot.name, "")
    return (
        func.length(StockSpot.code) == 6,
        or_(*(StockSpot.code.startswith(prefix) for prefix in prefixes)),
        ~func.upper(name).contains("ST"), ~name.contains("退"),
        ~invalid_tag, ~blacklisted,
    )


async def _reversal_ineligible_codes(db: AsyncSession, trade_date: date) -> set[str]:
    """轮次行情不回读实时StockSpot；资格只读同一策略使用的标记/黑名单。"""
    tagged = await db.scalars(select(StockTag.code).where(or_(
        StockTag.board_tag.is_(None), StockTag.board_tag != TAG_TRADEABLE,
        StockTag.is_st.is_(True), StockTag.is_suspended.is_(True),
        StockTag.is_delisting.is_(True),
    )))
    blocked = await db.scalars(select(StockBlacklist.code).where(
        StockBlacklist.reason.in_(["st", "delisting", "suspended"]),
        _active_blacklist_clause(trade_date),
    ))
    return set(tagged.all()) | set(blocked.all())


def _round_reversal_prefilter(spot, *, underwater: bool) -> bool:
    """与SQL预筛一致，主板资格/名称过滤先于截断；输入为轮次属性对象。"""
    code = str(getattr(spot, "code", "") or "").strip()
    name = str(getattr(spot, "name", "") or "")
    if len(code) != 6 or not code.isdigit() or not stock_tagger.is_tradeable(code):
        return False
    if "ST" in name.upper() or "退" in name:
        return False
    value = lambda name: _to_float(getattr(spot, name, None))
    price, previous, opening = value("price"), value("prev_close"), value("open")
    change, average, volume = value("change_pct"), value("avg_price"), value("volume_ratio")
    book = value("orderbook_imbalance")
    if (price is None or price <= 0 or previous is None or previous <= 0
        or opening is None or opening <= 0 or change is None
        or price <= previous or price <= opening or (average is not None and price < average)
        or volume is None):
        return False
    if underwater:
        low = value("low")
        return bool(
            low is not None and low > 0
            and low <= previous * (1 - settings.PAPER_AUTO_UNDERWATER_REVERSAL_MIN_LOW_DROP_PCT / 100)
            and settings.PAPER_AUTO_UNDERWATER_REVERSAL_MIN_CHANGE_PCT <= change <= settings.PAPER_AUTO_UNDERWATER_REVERSAL_MAX_CHANGE_PCT
            and volume >= settings.PAPER_AUTO_UNDERWATER_REVERSAL_MIN_VOLUME_RATIO
            and book is not None and book >= settings.PAPER_AUTO_UNDERWATER_REVERSAL_MIN_ORDERBOOK
        )
    return bool(
        opening <= previous * (1 - settings.PAPER_AUTO_GREEN_REVERSAL_OPEN_DROP_PCT / 100)
        and settings.PAPER_AUTO_GREEN_REVERSAL_MIN_CHANGE_PCT <= change <= settings.PAPER_AUTO_GREEN_REVERSAL_MAX_CHANGE_PCT
        and volume >= settings.PAPER_AUTO_GREEN_REVERSAL_MIN_VOLUME_RATIO
        and (book is None or book >= 0)
    )


async def _green_limit_reversal_candidates(
    db: AsyncSession,
    *,
    limit: int,
    trade_date: date,
) -> tuple[list[dict], list[str]]:
    if not settings.PAPER_AUTO_GREEN_REVERSAL_ENABLED:
        return [], []

    # 行情轮次模式使用 owned records；无轮次的手工调用仍走数据库预筛。
    if _quote_round_context().get("round_id"):
        ineligible = await _reversal_ineligible_codes(db, trade_date)
        rows = [spot for spot in _all_round_spots()
                if _round_reversal_prefilter(spot, underwater=False) and spot.code not in ineligible]
    else:
        rows = (
            await db.execute(
                select(StockSpot)
                .where(
                    *_reversal_universe_sql_filters(trade_date),
                    StockSpot.price.is_not(None), StockSpot.price > 0,
                    StockSpot.prev_close.is_not(None), StockSpot.prev_close > 0,
                    StockSpot.open.is_not(None), StockSpot.open > 0,
                    StockSpot.change_pct.is_not(None),
                    StockSpot.change_pct >= settings.PAPER_AUTO_GREEN_REVERSAL_MIN_CHANGE_PCT,
                    StockSpot.change_pct <= settings.PAPER_AUTO_GREEN_REVERSAL_MAX_CHANGE_PCT,
                    StockSpot.open <= StockSpot.prev_close * (1 - settings.PAPER_AUTO_GREEN_REVERSAL_OPEN_DROP_PCT / 100),
                    StockSpot.price > StockSpot.open,
                    StockSpot.price > StockSpot.prev_close,
                    or_(StockSpot.avg_price.is_(None), StockSpot.price >= StockSpot.avg_price),
                    StockSpot.volume_ratio.is_not(None),
                    StockSpot.volume_ratio >= settings.PAPER_AUTO_GREEN_REVERSAL_MIN_VOLUME_RATIO,
                    or_(StockSpot.orderbook_imbalance.is_(None), StockSpot.orderbook_imbalance >= 0),
                )
                # 真实资金资格和原资金排序在读取FundFlow后、截断前处理。
            )
        ).scalars().all()

    rows, funds, fund_notes = await _funded_reversal_rows(
        db, rows, trade_date=trade_date, limit=limit, underwater=False,
    )
    candidates: list[dict] = []
    rejected = 0
    for spot in rows:
        code = str(getattr(spot, "code", "") or "").strip()
        price = _to_float(getattr(spot, "price", None)) or 0.0
        prev_close = _to_float(getattr(spot, "prev_close", None)) or 0.0
        open_price = _to_float(getattr(spot, "open", None)) or 0.0
        low = _to_float(getattr(spot, "low", None)) or 0.0
        high = _to_float(getattr(spot, "high", None)) or 0.0
        change_pct = _to_float(getattr(spot, "change_pct", None))
        avg_price = _to_float(getattr(spot, "avg_price", None))
        volume_ratio = _to_float(getattr(spot, "volume_ratio", None))
        fund = funds.get(code)
        main_net_inflow = _to_float((fund or {}).get("main_net_inflow"))
        orderbook_imbalance = _to_float(getattr(spot, "orderbook_imbalance", None))
        min5_change = _paper_effective_min5_change(spot, trade_date=trade_date)
        if not code or price <= 0 or prev_close <= 0 or open_price <= 0 or change_pct is None:
            rejected += 1
            continue
        name = getattr(spot, "name", None) or code
        if "ST" in str(name).upper() or "退" in str(name):
            rejected += 1
            continue
        open_change_pct = (open_price / prev_close - 1) * 100
        low_open = open_change_pct <= -settings.PAPER_AUTO_GREEN_REVERSAL_OPEN_DROP_PCT
        if (
            not low_open
            or change_pct < settings.PAPER_AUTO_GREEN_REVERSAL_MIN_CHANGE_PCT
            or change_pct > settings.PAPER_AUTO_GREEN_REVERSAL_MAX_CHANGE_PCT
        ):
            rejected += 1
            continue
        if price <= open_price or price <= prev_close:
            rejected += 1
            continue
        if avg_price is not None and price < avg_price:
            rejected += 1
            continue
        if volume_ratio is None or volume_ratio < settings.PAPER_AUTO_GREEN_REVERSAL_MIN_VOLUME_RATIO:
            rejected += 1
            continue
        if main_net_inflow is None or main_net_inflow <= 0:
            rejected += 1
            continue
        if orderbook_imbalance is not None and orderbook_imbalance < 0:
            rejected += 1
            continue

        close_position = (
            (price - low) / (high - low)
            if low > 0 and high > low
            else None
        )
        avg_premium_pct = (
            (price / avg_price - 1) * 100
            if avg_price is not None and avg_price > 0
            else None
        )
        rebound_from_low_pct = (
            (price / low - 1) * 100
            if low > 0
            else None
        )
        retest_confirmation = _reversal_retest_confirmed(
            change_pct=change_pct,
            avg_premium_pct=avg_premium_pct,
            close_position=close_position,
            min5_change=min5_change,
        )

        sector_ctx, sector_reason = await _latest_sector_context_for_code(
            db,
            code,
            trade_date=trade_date,
        )
        sector_strength = _to_float(sector_ctx.get("sector_strength")) or 0.0
        sector_change_pct = _to_float(sector_ctx.get("sector_change_pct")) or 0.0
        sector_fund_flow = _to_float(sector_ctx.get("sector_fund_flow")) or 0.0
        limit_up_count = int(sector_ctx.get("sector_limit_up_count") or 0)
        leader_first_move_reason = _green_reversal_leader_first_move_reason(
            change_pct=change_pct,
            sector_change_pct=sector_change_pct,
            volume_ratio=volume_ratio,
            orderbook_imbalance=orderbook_imbalance,
        )
        theme_spread = limit_up_count >= settings.PAPER_AUTO_GREEN_REVERSAL_LEADER_MIN_LIMIT_UP_COUNT
        leader_has_sector_support = (
            bool(leader_first_move_reason)
            and (
                theme_spread
                or (
                    sector_strength >= settings.PAPER_AUTO_GREEN_REVERSAL_LEADER_MIN_SECTOR_STRENGTH
                    and sector_fund_flow > 0
                )
            )
        )
        if (
            not leader_has_sector_support
            and (
                sector_strength < settings.PAPER_AUTO_GREEN_REVERSAL_MIN_SECTOR_STRENGTH
                or sector_change_pct < settings.PAPER_AUTO_GREEN_REVERSAL_MIN_SECTOR_CHANGE_PCT
                or sector_fund_flow <= 0
            )
        ):
            rejected += 1
            continue
        score = 88.0
        score += min(3.0, max(0.0, change_pct - settings.PAPER_AUTO_GREEN_REVERSAL_MIN_CHANGE_PCT) * 0.5)
        score += min(2.0, max(0.0, volume_ratio - settings.PAPER_AUTO_GREEN_REVERSAL_MIN_VOLUME_RATIO))
        if orderbook_imbalance is not None:
            score += min(2.0, max(0.0, orderbook_imbalance) * 2)
        seal_quality = _to_float(getattr(spot, "seal_quality_score", None))
        if seal_quality is not None:
            score += min(1.0, max(0.0, seal_quality) / 100)

        candidates.append({
            "_source": "green_limit_reversal",
            "signal_source": _PAPER_BUY_SOURCE_LABELS["green_limit_reversal"],
            "code": code,
            "name": name,
            "total_score": min(99.0, score),
            "price": price,
            "change_pct": change_pct,
            "open_price": open_price,
            "open_change_pct": open_change_pct,
            "prev_close": prev_close,
            "low_price": low,
            "high_price": high,
            "avg_price": avg_price,
            "avg_premium_pct": round(avg_premium_pct, 2) if avg_premium_pct is not None else None,
            "rebound_from_low_pct": round(rebound_from_low_pct, 2) if rebound_from_low_pct is not None else None,
            "close_position": round(close_position, 3) if close_position is not None else None,
            "min5_change": min5_change,
            "volume_ratio": volume_ratio,
            **_main_fund_evidence(fund),
            "orderbook_imbalance": orderbook_imbalance,
            "leader_first_move": bool(leader_first_move_reason),
            "leader_first_move_reason": leader_first_move_reason,
            "theme_spread": theme_spread,
            "execution_confirmation": retest_confirmation,
            "retest_confirmation": retest_confirmation,
            "stop_loss_price": _default_stop_loss_price(price),
            "sector_reason": sector_reason,
            **sector_ctx,
        })

    if rejected:
        fund_notes.append(f"绿盘转强扫描过滤{rejected}只未低开转强/涨幅不在低吸区间/资金盘口不达标标的")
    return candidates[:limit], fund_notes


async def _underwater_reversal_candidates(
    db: AsyncSession,
    *,
    limit: int,
    trade_date: date,
) -> tuple[list[dict], list[str]]:
    if not settings.PAPER_AUTO_UNDERWATER_REVERSAL_ENABLED:
        return [], []

    # 同一报价轮次内直接基于不可变快照筛选，避免不同账户在串行查询中读到
    # 已被下一轮覆盖的 StockSpot。脱离报价轮次调用时保留数据库回退路径。
    round_ctx = _quote_round_context()
    if round_ctx.get("round_id"):
        ineligible = await _reversal_ineligible_codes(db, trade_date)
        rows = [spot for spot in _all_round_spots()
                if _round_reversal_prefilter(spot, underwater=True) and spot.code not in ineligible]
        # 先匹配真实资金，再按原排序键与上限截断。
    else:
        # 水下幅度、翻红价格与资金盘口先进入 SQL 预筛选，避免资金流靠前但
        # 从未水下的普涨股占满扫描上限，导致真正的水下翻红标的召回为零。
        rows = (
            await db.execute(
                select(StockSpot)
                .where(
                    *_reversal_universe_sql_filters(trade_date),
                    StockSpot.price.is_not(None),
                    StockSpot.price > 0,
                    StockSpot.prev_close.is_not(None),
                    StockSpot.prev_close > 0,
                    StockSpot.open.is_not(None),
                    StockSpot.open > 0,
                    StockSpot.low.is_not(None),
                    StockSpot.low > 0,
                    StockSpot.change_pct.is_not(None),
                    StockSpot.low <= StockSpot.prev_close * (
                        1 - settings.PAPER_AUTO_UNDERWATER_REVERSAL_MIN_LOW_DROP_PCT / 100
                    ),
                    StockSpot.change_pct >= settings.PAPER_AUTO_UNDERWATER_REVERSAL_MIN_CHANGE_PCT,
                    StockSpot.change_pct <= settings.PAPER_AUTO_UNDERWATER_REVERSAL_MAX_CHANGE_PCT,
                    StockSpot.price > StockSpot.prev_close,
                    StockSpot.price > StockSpot.open,
                    or_(StockSpot.avg_price.is_(None), StockSpot.price >= StockSpot.avg_price),
                    StockSpot.volume_ratio.is_not(None),
                    StockSpot.volume_ratio >= settings.PAPER_AUTO_UNDERWATER_REVERSAL_MIN_VOLUME_RATIO,
                    StockSpot.orderbook_imbalance.is_not(None),
                    StockSpot.orderbook_imbalance >= settings.PAPER_AUTO_UNDERWATER_REVERSAL_MIN_ORDERBOOK,
                )
                # 不以StockSpot旧委差进行资金预筛/排序。
            )
        ).scalars().all()

    rows, funds, fund_notes = await _funded_reversal_rows(
        db, rows, trade_date=trade_date, limit=limit, underwater=True,
    )
    candidates: list[dict] = []
    rejected = 0
    for spot in rows:
        code = str(getattr(spot, "code", "") or "").strip()
        price = _to_float(getattr(spot, "price", None)) or 0.0
        prev_close = _to_float(getattr(spot, "prev_close", None)) or 0.0
        open_price = _to_float(getattr(spot, "open", None)) or 0.0
        low = _to_float(getattr(spot, "low", None)) or 0.0
        high = _to_float(getattr(spot, "high", None)) or 0.0
        change_pct = _to_float(getattr(spot, "change_pct", None))
        avg_price = _to_float(getattr(spot, "avg_price", None))
        volume_ratio = _to_float(getattr(spot, "volume_ratio", None))
        fund = funds.get(code)
        main_net_inflow = _to_float((fund or {}).get("main_net_inflow"))
        orderbook_imbalance = _to_float(getattr(spot, "orderbook_imbalance", None))
        min5_change = _paper_effective_min5_change(spot, trade_date=trade_date)
        if not code or price <= 0 or prev_close <= 0 or open_price <= 0 or low <= 0 or change_pct is None:
            rejected += 1
            continue
        name = getattr(spot, "name", None) or code
        if "ST" in str(name).upper() or "退" in str(name):
            rejected += 1
            continue
        low_drop_pct = (low / prev_close - 1) * 100
        open_change_pct = (open_price / prev_close - 1) * 100
        if low_drop_pct > -settings.PAPER_AUTO_UNDERWATER_REVERSAL_MIN_LOW_DROP_PCT:
            rejected += 1
            continue
        if (
            change_pct < settings.PAPER_AUTO_UNDERWATER_REVERSAL_MIN_CHANGE_PCT
            or change_pct > settings.PAPER_AUTO_UNDERWATER_REVERSAL_MAX_CHANGE_PCT
        ):
            rejected += 1
            continue
        if price <= prev_close or price <= open_price:
            rejected += 1
            continue
        if avg_price is not None and price < avg_price:
            rejected += 1
            continue
        if volume_ratio is None or volume_ratio < settings.PAPER_AUTO_UNDERWATER_REVERSAL_MIN_VOLUME_RATIO:
            rejected += 1
            continue
        if main_net_inflow is None or main_net_inflow <= 0:
            rejected += 1
            continue
        if (
            orderbook_imbalance is None
            or orderbook_imbalance < settings.PAPER_AUTO_UNDERWATER_REVERSAL_MIN_ORDERBOOK
        ):
            rejected += 1
            continue
        close_position = None
        pullback_from_high_pct = 0.0
        if high > low and price > 0:
            close_position = round((price - low) / (high - low), 2)
            pullback_from_high_pct = (price / high - 1) * 100 if high > 0 else 0.0
        if (
            high > price
            and pullback_from_high_pct <= -settings.PAPER_AUTO_UNDERWATER_REVERSAL_MAX_PULLBACK_FROM_HIGH_PCT
        ):
            rejected += 1
            continue
        if (
            close_position is not None
            and close_position < settings.PAPER_AUTO_UNDERWATER_REVERSAL_MIN_CLOSE_POSITION
        ):
            rejected += 1
            continue
        if min5_change is not None and min5_change < settings.PAPER_AUTO_UNDERWATER_REVERSAL_MIN5_CHANGE_FLOOR:
            rejected += 1
            continue

        avg_premium_pct = (
            (price / avg_price - 1) * 100
            if avg_price is not None and avg_price > 0
            else None
        )
        retest_confirmation = _reversal_retest_confirmed(
            change_pct=change_pct,
            avg_premium_pct=avg_premium_pct,
            close_position=close_position,
            min5_change=min5_change,
        )

        sector_ctx, sector_reason = await _latest_sector_context_for_code(
            db,
            code,
            trade_date=trade_date,
        )
        sector_strength = _to_float(sector_ctx.get("sector_strength")) or 0.0
        sector_change_pct = _to_float(sector_ctx.get("sector_change_pct")) or 0.0
        sector_fund_flow = _to_float(sector_ctx.get("sector_fund_flow")) or 0.0
        limit_up_count = int(sector_ctx.get("sector_limit_up_count") or 0)
        leader_first_move_reason = _green_reversal_leader_first_move_reason(
            change_pct=change_pct,
            sector_change_pct=sector_change_pct,
            volume_ratio=volume_ratio,
            orderbook_imbalance=orderbook_imbalance,
        )
        theme_breadth_only = (
            limit_up_count >= settings.PAPER_AUTO_UNDERWATER_REVERSAL_MIN_LIMIT_UP_COUNT
        )
        theme_spread = bool(
            theme_breadth_only
            and sector_strength
            >= settings.PAPER_AUTO_UNDERWATER_REVERSAL_MIN_SECTOR_STRENGTH
            and sector_change_pct
            >= settings.PAPER_AUTO_UNDERWATER_REVERSAL_MIN_SECTOR_CHANGE_PCT
            and (
                not settings.PAPER_AUTO_UNDERWATER_REVERSAL_REQUIRE_POSITIVE_FUND_FLOW
                or sector_fund_flow > 0
            )
        )
        if (
            not leader_first_move_reason
            and not theme_spread
            and (
                sector_strength
                < settings.PAPER_AUTO_UNDERWATER_REVERSAL_MIN_SECTOR_STRENGTH
                or sector_change_pct
                < settings.PAPER_AUTO_UNDERWATER_REVERSAL_MIN_SECTOR_CHANGE_PCT
                or (
                    settings.PAPER_AUTO_UNDERWATER_REVERSAL_REQUIRE_POSITIVE_FUND_FLOW
                    and sector_fund_flow <= 0
                )
            )
        ):
            rejected += 1
            continue

        score = 87.0
        score += min(3.0, abs(low_drop_pct) * 0.45)
        score += min(2.0, max(0.0, change_pct))
        score += min(2.0, max(0.0, volume_ratio - settings.PAPER_AUTO_UNDERWATER_REVERSAL_MIN_VOLUME_RATIO))
        score += min(2.0, max(0.0, orderbook_imbalance) * 2)
        if theme_spread:
            score += 1.0

        candidates.append({
            "_source": "underwater_reversal",
            "signal_source": _PAPER_BUY_SOURCE_LABELS["underwater_reversal"],
            "code": code,
            "name": name,
            "total_score": min(99.0, score),
            "theme_breadth_only": theme_breadth_only,
            "price": price,
            "change_pct": change_pct,
            "open_price": open_price,
            "open_change_pct": open_change_pct,
            "low_price": low,
            "high_price": high,
            "low_drop_pct": low_drop_pct,
            "pullback_from_high_pct": pullback_from_high_pct,
            "close_position": close_position,
            "prev_close": prev_close,
            "avg_price": avg_price,
            "avg_premium_pct": round(avg_premium_pct, 2) if avg_premium_pct is not None else None,
            "volume_ratio": volume_ratio,
            **_main_fund_evidence(fund),
            "orderbook_imbalance": orderbook_imbalance,
            "min5_change": min5_change,
            "leader_first_move": bool(leader_first_move_reason),
            "leader_first_move_reason": leader_first_move_reason,
            "theme_spread": theme_spread,
            "execution_confirmation": retest_confirmation,
            "retest_confirmation": retest_confirmation,
            "stop_loss_price": _default_stop_loss_price(price),
            "sector_reason": sector_reason,
            **sector_ctx,
        })

    candidates.sort(key=lambda item: _to_float(item.get("total_score")) or 0, reverse=True)
    if rejected:
        fund_notes.append(f"水下翻红扫描过滤{rejected}只未水下承接/翻红强度/资金盘口不达标标的")
    return candidates[:limit], fund_notes


async def _restore_armed_reversal_candidates(
    db: AsyncSession,
    *,
    account_id: int,
    trade_date: date,
    existing_candidates: list[dict],
    limit: int,
    observed_at: Optional[datetime] = None,
) -> tuple[list[dict], list[str]]:
    """Restore A-strategy arms that left the trigger band before the first retest.

    ``PaperAutoTradeLog`` is append-only and already freezes the point-in-time
    candidate.  Reusing it avoids an in-memory flag that would disappear after a
    worker restart.  A restored arm is never executable merely because it once
    rallied: the current quote must independently pass the zero-axis/VWAP/range
    retest and the original capital/sector gates.
    """

    if account_id <= 0 or limit <= 0:
        return [], []
    now = observed_at or _paper_now()
    if now.date() != trade_date:
        return [], []
    ttl_minutes = max(1, int(settings.PAPER_AUTO_REVERSAL_ARM_TTL_MINUTES))
    cutoff = max(
        datetime.combine(trade_date, time.min),
        now - timedelta(minutes=ttl_minutes),
    )
    existing_codes = {
        str(item.get("code") or "").strip()
        for item in existing_candidates
        if str(item.get("code") or "").strip()
    }
    logs = list(
        (
            await db.scalars(
                select(PaperAutoTradeLog)
                .where(
                    PaperAutoTradeLog.account_id == account_id,
                    PaperAutoTradeLog.trade_date == trade_date,
                    PaperAutoTradeLog.created_at >= cutoff,
                    PaperAutoTradeLog.created_at <= now,
                    PaperAutoTradeLog.source.in_(
                        ("green_limit_reversal", "underwater_reversal")
                    ),
                    PaperAutoTradeLog.code.is_not(None),
                )
                .order_by(desc(PaperAutoTradeLog.created_at), desc(PaperAutoTradeLog.id))
                .limit(max(limit * 20, 100))
            )
        ).all()
    )
    restored: list[dict] = []
    fund_notes: list[str] = []
    seen = set(existing_codes)
    for log in logs:
        code = str(log.code or "").strip()
        if not code or code in seen or not stock_tagger.is_tradeable(code):
            continue
        # 只消费同股最新状态；较新的 confirmed/失效日志不能被更旧的 armed
        # 记录覆盖，否则状态机会倒退并重复恢复。
        seen.add(code)
        armed = _json_loads_dict(log.candidate_json)
        source = str(armed.get("_source") or log.source or "")
        if source not in {"green_limit_reversal", "underwater_reversal"}:
            continue
        # Confirmed historical candidates are not arms; they are handled by the
        # normal duplicate/order controls and must not be resurrected.
        if armed.get("execution_confirmation"):
            continue

        spot = await _spot_by_code(db, code)
        if spot is None:
            continue
        price = _to_float(spot.price) or 0.0
        prev_close = _to_float(spot.prev_close) or 0.0
        open_price = _to_float(spot.open) or 0.0
        low = _to_float(spot.low) or 0.0
        high = _to_float(spot.high) or 0.0
        avg_price = _to_float(spot.avg_price)
        change_pct = _to_float(spot.change_pct)
        volume_ratio = _to_float(spot.volume_ratio)
        fund = (await _paper_main_fund_map(db, trade_date=trade_date, codes=[code], decision_at=now)).get(code)
        if fund is None:
            fund_notes.append(f"{code}回踩恢复真实主力资金unknown（缺失/过期/不可见）")
        main_net_inflow = _to_float((fund or {}).get("main_net_inflow"))
        orderbook_imbalance = _to_float(spot.orderbook_imbalance)
        name = str(spot.name or armed.get("name") or code)
        if (
            price <= 0
            or prev_close <= 0
            or open_price <= 0
            or low <= 0
            or high <= low
            or change_pct is None
            or "ST" in name.upper()
            or "退" in name
            or price <= prev_close
            or price <= open_price
            or avg_price is None
            or avg_price <= 0
            or price < avg_price
            or main_net_inflow is None
            or main_net_inflow <= 0
        ):
            continue

        open_change_pct = (open_price / prev_close - 1.0) * 100.0
        low_drop_pct = (low / prev_close - 1.0) * 100.0
        if source == "green_limit_reversal":
            if (
                open_change_pct > -settings.PAPER_AUTO_GREEN_REVERSAL_OPEN_DROP_PCT
                or volume_ratio is None
                or volume_ratio < settings.PAPER_AUTO_GREEN_REVERSAL_MIN_VOLUME_RATIO
                or (orderbook_imbalance is not None and orderbook_imbalance < 0)
            ):
                continue
        else:
            if (
                low_drop_pct > -settings.PAPER_AUTO_UNDERWATER_REVERSAL_MIN_LOW_DROP_PCT
                or volume_ratio is None
                or volume_ratio < settings.PAPER_AUTO_UNDERWATER_REVERSAL_MIN_VOLUME_RATIO
                or orderbook_imbalance is None
                or orderbook_imbalance
                < settings.PAPER_AUTO_UNDERWATER_REVERSAL_MIN_ORDERBOOK
            ):
                continue

        close_position = (price - low) / (high - low)
        avg_premium_pct = (price / avg_price - 1.0) * 100.0
        min5_change = _paper_effective_min5_change(spot, trade_date=trade_date)
        if not _reversal_retest_confirmed(
            change_pct=change_pct,
            avg_premium_pct=avg_premium_pct,
            close_position=close_position,
            min5_change=min5_change,
        ):
            continue

        sector_ctx, sector_reason = await _latest_sector_context_for_code(
            db,
            code,
            trade_date=trade_date,
        )
        sector_strength = _to_float(sector_ctx.get("sector_strength")) or 0.0
        sector_change_pct = _to_float(sector_ctx.get("sector_change_pct")) or 0.0
        sector_fund_flow = _to_float(sector_ctx.get("sector_fund_flow")) or 0.0
        limit_up_count = int(sector_ctx.get("sector_limit_up_count") or 0)
        leader_reason = _green_reversal_leader_first_move_reason(
            change_pct=change_pct,
            sector_change_pct=sector_change_pct,
            volume_ratio=volume_ratio,
            orderbook_imbalance=orderbook_imbalance,
        )
        if source == "green_limit_reversal":
            theme_spread = (
                limit_up_count
                >= settings.PAPER_AUTO_GREEN_REVERSAL_LEADER_MIN_LIMIT_UP_COUNT
            )
            sector_ok = bool(leader_reason) and (
                theme_spread
                or (
                    sector_strength
                    >= settings.PAPER_AUTO_GREEN_REVERSAL_LEADER_MIN_SECTOR_STRENGTH
                    and sector_fund_flow > 0
                )
            )
            sector_ok = sector_ok or (
                sector_strength >= settings.PAPER_AUTO_GREEN_REVERSAL_MIN_SECTOR_STRENGTH
                and sector_change_pct
                >= settings.PAPER_AUTO_GREEN_REVERSAL_MIN_SECTOR_CHANGE_PCT
                and sector_fund_flow > 0
            )
        else:
            theme_spread = (
                limit_up_count
                >= settings.PAPER_AUTO_UNDERWATER_REVERSAL_MIN_LIMIT_UP_COUNT
            )
            sector_ok = bool(leader_reason) or theme_spread or (
                sector_strength >= settings.PAPER_AUTO_GREEN_REVERSAL_MIN_SECTOR_STRENGTH
                and sector_change_pct >= 0
                and sector_fund_flow > 0
            )
        if not sector_ok:
            continue

        restored_candidate = {
            **armed,
            **sector_ctx,
            "_source": source,
            "signal_source": _PAPER_BUY_SOURCE_LABELS[source],
            "code": code,
            "name": name,
            "price": price,
            "change_pct": change_pct,
            "open_price": open_price,
            "open_change_pct": open_change_pct,
            "prev_close": prev_close,
            "low_price": low,
            "high_price": high,
            "low_drop_pct": low_drop_pct,
            "avg_price": avg_price,
            "avg_premium_pct": round(avg_premium_pct, 2),
            "close_position": round(close_position, 3),
            "min5_change": min5_change,
            "volume_ratio": volume_ratio,
            **_main_fund_evidence(fund),
            "orderbook_imbalance": orderbook_imbalance,
            "leader_first_move": bool(leader_reason),
            "leader_first_move_reason": leader_reason,
            "theme_spread": theme_spread,
            "execution_confirmation": True,
            "retest_confirmation": True,
            "reversal_state": "retest_confirmed_from_persisted_arm",
            "armed_at": log.created_at.isoformat(timespec="seconds"),
            "armed_change_pct": _to_float(armed.get("change_pct")),
            "stop_loss_price": _default_stop_loss_price(price),
            "sector_reason": sector_reason,
        }
        restored.append(restored_candidate)
        if len(restored) >= limit:
            break

    notes = (
        [f"策略A从当日持久化触发状态恢复{len(restored)}只零轴/VWAP回踩确认候选"]
        if restored
        else []
    )
    return restored, [*notes, *fund_notes]


async def _anomaly_buy_point_candidates(
    db: AsyncSession,
    *,
    limit: int,
    trade_date: date,
) -> tuple[list[dict], list[str]]:
    notes: list[str] = []
    try:
        from app.api.v1.tenbagger import (
            _apply_stock_row_buy_point_status,
            _build_stock_rows,
            _compare_aggregated_rows,
            _enrich_stock_rows_with_b1,
            prewarm_anomaly_snapshot,
        )

        snapshot = await prewarm_anomaly_snapshot(db, None, force_refresh=False)
    except Exception as exc:
        await db.rollback()
        return [], [f"异动买点加载失败：{exc}"]

    snapshot_date = None
    try:
        snapshot_date = date.fromisoformat(str(snapshot.get("trade_date") or ""))
    except ValueError:
        snapshot_date = None
    if not await _is_signal_snapshot_valid_for_trade(snapshot_date, trade_date):
        return [], [f"异动买点最新信号日为{snapshot.get('trade_date') or '--'}，不是今日可执行信号"]

    anomalies = [
        item for item in (snapshot.get("anomalies") or [])
        if (_to_float(item.get("score")) or 0) >= 50
    ]
    rows = _build_stock_rows(anomalies, sort_by="buy_point")
    try:
        rows = await _enrich_stock_rows_with_b1(rows, db, target_date=snapshot_date)
    except Exception:
        await db.rollback()
    funds = await _paper_main_fund_map(db, trade_date=trade_date, codes=[row.get("code") for row in rows])
    # 外部页面增强默认当前时刻；这里恢复本轮可见资金，不回写页面/行情快照。
    rows = [dict(row) for row in rows]
    for row in rows:
        _bind_main_fund_evidence(row, funds.get(str(row.get("code") or "")))
    rows = [_apply_stock_row_buy_point_status(row) for row in rows]

    candidates: list[dict] = []
    rejected = 0
    rejected_risk = 0
    now_time = datetime.now().time()
    for row in rows:
        if not (row.get("buy_point_pushable") or row.get("buy_point_reached")):
            continue
        detail = row.get("detail") or {}
        sector, sector_reason = _active_sector_from_anomaly_detail(detail)
        if not sector:
            rejected += 1
            continue
        code = str(row.get("code") or "").strip()
        strength = _to_float(sector.get("strength_score")) or 0.0
        sector_change_pct = _to_float(sector.get("change_pct")) or 0.0
        if _anomaly_sector_reject_reason(strength, sector_change_pct):
            rejected_risk += 1
            continue
        if now_time >= time(14, 30):
            rejected_risk += 1
            continue
        price = _to_float(row.get("current_price")) or _to_float(detail.get("price")) or 0.0
        change_pct = _to_float(row.get("change_pct")) or _to_float(detail.get("change_pct")) or 0.0
        if change_pct >= settings.PAPER_AUTO_ANOMALY_MAX_INTRADAY_CHASE_PCT:
            rejected_risk += 1
            continue
        avg_price = _to_float(row.get("avg_price")) or _to_float(detail.get("avg_price"))
        avg_premium_pct = (price / avg_price - 1) * 100 if price > 0 and avg_price and avg_price > 0 else 0.0
        if (
            sector_change_pct < settings.PAPER_AUTO_ANOMALY_MIN_SECTOR_CHANGE_PCT
            and strength < settings.PAPER_AUTO_ANOMALY_STRONG_SECTOR_OVERRIDE
            and avg_premium_pct > settings.PAPER_AUTO_ANOMALY_WEAK_SECTOR_MAX_AVG_PREMIUM_PCT
        ):
            rejected_risk += 1
            continue
        recent_change = await _recent_kline_change_pct(db, code, limit=3)
        if recent_change is not None and recent_change >= settings.PAPER_AUTO_ANOMALY_MAX_3D_CHANGE_PCT:
            rejected_risk += 1
            continue
        rebound_reason = await _weak_rebound_reject_reason(
            db,
            code=code,
            trade_date=trade_date,
            price=price,
            change_pct=change_pct,
            event_types=row.get("event_types") or [],
        )
        if rebound_reason:
            rejected_risk += 1
            continue
        if now_time >= time(10, 30) and strength < settings.PAPER_AUTO_ANOMALY_MIN_SECTOR_STRENGTH + 5:
            rejected_risk += 1
            continue
        b1_stop = _to_float(row.get("b1_trend_white_price"))
        stop_loss = b1_stop if b1_stop and price > 0 and b1_stop < price else _default_stop_loss_price(price)
        score = _to_float(row.get("priority_score")) or _to_float(row.get("display_score")) or _to_float(row.get("score")) or 0.0
        position_score = _anomaly_position_score(score, strength, sector_change_pct)
        candidate = {
            **row,
            "_source": "anomaly_buy_point",
            "signal_source": _PAPER_BUY_SOURCE_LABELS["anomaly_buy_point"],
            "raw_total_score": min(99.0, score),
            "total_score": position_score,
            "price": price,
            "sector_code": sector.get("sector_code") or "",
            "sector_name": sector.get("sector_name") or row.get("driver_primary") or "",
            "sector_strength": _to_float(sector.get("strength_score")) or 0.0,
            "sector_change_pct": sector_change_pct,
            "sector_fund_flow": _to_float(sector.get("fund_flow")) or 0.0,
            "avg_premium_pct": avg_premium_pct,
            "recent_3d_change_pct": recent_change,
            "rebound_check": "passed",
            "stop_loss_price": round(stop_loss, 2) if stop_loss else 0.0,
            "sector_reason": sector_reason,
        }
        candidates.append(candidate)

    if rejected:
        notes.append(f"已过滤{rejected}只未形成主线合力的异动买点")
    if rejected_risk:
        notes.append(f"已过滤{rejected_risk}只板块强度/涨幅位置/尾盘时间不适合自动追涨的异动买点")
    candidates.sort(key=lambda row: _compare_aggregated_rows(row, "buy_point"), reverse=True)
    return candidates[:limit], notes


async def _paper_auto_buy_candidates(
    db: AsyncSession,
    *,
    limit: int,
    trade_date: date,
    account_id: Optional[int] = None,
    include_daily_participation: bool = False,
    include_icepoint_reversal: bool = False,
) -> tuple[list[dict], list[str]]:
    plan_candidates, plan_notes = await _next_day_plan_buy_candidates(db, limit=limit, trade_date=trade_date)
    green_candidates, green_notes = await _green_limit_reversal_candidates(db, limit=limit, trade_date=trade_date)
    underwater_candidates, underwater_notes = await _underwater_reversal_candidates(db, limit=limit, trade_date=trade_date)
    ma5_candidates, ma5_notes = await _ma5_pullback_candidates(db, limit=limit, trade_date=trade_date)
    anomaly_candidates, anomaly_notes = await _anomaly_buy_point_candidates(db, limit=limit, trade_date=trade_date)
    daily_candidates: list[dict] = []
    daily_notes: list[str] = []
    if include_daily_participation:
        daily_candidates = await _daily_participation_candidates(db, limit=limit, trade_date=trade_date)
        if daily_candidates:
            daily_notes.append("今日尚未开仓，市场不弱，启用每日参与保障候选")
    icepoint_candidates: list[dict] = []
    icepoint_notes: list[str] = []
    if include_icepoint_reversal:
        icepoint_candidates = await _icepoint_reversal_candidates(db, limit=limit, trade_date=trade_date)
        if icepoint_candidates:
            icepoint_notes.append("市场处于冰点/弱修复区，启用冰点转强候选")
    restored_candidates: list[dict] = []
    restored_notes: list[str] = []
    if account_id is not None:
        restored_candidates, restored_notes = await _restore_armed_reversal_candidates(
            db,
            account_id=account_id,
            trade_date=trade_date,
            existing_candidates=[*green_candidates, *underwater_candidates],
            limit=limit,
        )
    all_candidates = [
        *plan_candidates,
        *green_candidates,
        *underwater_candidates,
        *restored_candidates,
        *ma5_candidates,
        *anomaly_candidates,
        *icepoint_candidates,
        *daily_candidates,
    ]
    # 外部计划/异动中的旧主力字段不是当前资金；独立冻结合格资金，不改原rank。
    funds = await _paper_main_fund_map(
        db, trade_date=trade_date, codes=[item.get("code") for item in all_candidates],
    )
    for candidate in all_candidates:
        _bind_main_fund_evidence(candidate, funds.get(str(candidate.get("code") or "")))
    # 候选排序先看位置赔率，再看总分。评分描述“强度”，不能让高分追涨
    # 排在已确认的回踩/水下回收之前。
    source_rank = {
        "ma5_pullback": 5,
        "underwater_reversal": 5,
        "icepoint_reversal": 5,
        "green_limit_reversal": 4,
        "next_day_plan": 3,
        "anomaly_buy_point": 2,
        "daily_participation": 5,
    }

    def entry_value_rank(item: dict) -> float:
        source = str(item.get("_source") or "")
        rank = float(source_rank.get(source, 0))
        if source in {
            "ma5_pullback",
            "underwater_reversal",
            "green_limit_reversal",
            "icepoint_reversal",
            "daily_participation",
        }:
            rank += 2.0 if item.get("execution_confirmation") else -10.0
        change_pct = _to_float(item.get("change_pct"))
        if change_pct is not None:
            if change_pct <= 1.0:
                rank += 2.0
            elif change_pct <= 2.0:
                rank += 1.0
            elif change_pct > settings.PAPER_AUTO_VALUE_ENTRY_MAX_CHANGE_PCT:
                rank -= 4.0
        avg_premium_pct = _to_float(item.get("avg_premium_pct"))
        if avg_premium_pct is not None and -0.3 <= avg_premium_pct <= 0.8:
            rank += 1.0
        return rank

    all_candidates.sort(
        key=lambda item: (
            entry_value_rank(item),
            _to_float(item.get("total_score")) or 0,
            _to_float(item.get("sector_strength")) or 0,
        ),
        reverse=True,
    )
    # 同一只股票可能同时属于明日预案和盘中观察池。先按执行价值排序，再去重，
    # 防止一个尚待确认的高分预案覆盖已经完成VWAP/量能回收确认的盘中候选。
    merged: list[dict] = []
    seen: set[str] = set()
    for candidate in all_candidates:
        code = str(candidate.get("code") or "").strip()
        if not code or code in seen:
            continue
        seen.add(code)
        merged.append(candidate)
    return merged[:limit], [
        *plan_notes,
        *green_notes,
        *underwater_notes,
        *restored_notes,
        *ma5_notes,
        *anomaly_notes,
        *icepoint_notes,
        *daily_notes,
    ]


# =========================================================================
# 策略B: 晋级预测二板赛道 (Challenger 并行观察, 2026-08-31 新增)
# =========================================================================

async def _promotion_mainline_live_sector_context(
    db: AsyncSession,
    *,
    code: str,
    factors: dict,
) -> dict:
    """取快照声明的同一板块实时上下文，禁止用股票所属的另一强板块冒充确认。"""
    contexts = await _ranked_sector_contexts_for_code(db, code)
    requested_code = str(factors.get("sector_code") or "").strip()
    requested_name = str(factors.get("sector_name") or "").strip()
    selected = next(
        (
            item for item in contexts
            if requested_code and str(item.get("sector_code") or "") == requested_code
        ),
        None,
    )
    if selected is None and requested_name:
        selected = next(
            (
                item for item in contexts
                if requested_name in {
                    str(item.get("sector_name") or ""),
                    str(item.get("_mapping_name") or ""),
                }
            ),
            None,
        )
    if selected is None:
        return {}
    result = dict(selected)
    persistence = result.pop("_persistence", None)
    result.pop("_mapping_name", None)
    sector_trade_date = getattr(persistence, "trade_date", None)
    result["sector_trade_date"] = (
        sector_trade_date.isoformat()
        if isinstance(sector_trade_date, date)
        else ""
    )
    return result


def _promotion_snapshot_probability(record) -> float:
    """Consume frozen evidence; only genuinely old snapshots use the old column."""
    try:
        factors = json.loads(getattr(record, "features_json", None) or "{}")
    except (TypeError, ValueError):
        raise ProbabilityContractError("features_json: invalid_probability_evidence") from None
    if not isinstance(factors, dict):
        raise ProbabilityContractError("features_json: invalid_probability_evidence")
    fields = ("probability_contract_version", "p_raw", "p_calibrated", "production_probability")
    if "probability_contract" in factors:
        evidence = factors["probability_contract"]
        if not isinstance(evidence, dict) or "probability_contract_version" not in evidence:
            raise ProbabilityContractError("probability_contract: invalid_evidence")
        resolved = resolve_promotion_probability(evidence, allow_legacy=True)
        if any(field in factors for field in fields):
            flat = resolve_promotion_probability(factors, allow_legacy=True)
            if flat != resolved:
                raise ProbabilityContractError("probability_contract: conflicting_evidence")
        return resolved.production_probability
    if any(field in factors for field in fields):
        return resolve_promotion_probability(factors, allow_legacy=True).production_probability
    # Explicit old-protocol boundary, not a fallback from invalid new evidence.
    return resolve_promotion_probability(
        {"probability": getattr(record, "calibrated_probability", None)},
        allow_legacy=True,
    ).production_probability


def _promotion_mainline_live_confirm_reject_reason(
    record,
    latest_run,
    *,
    trade_date: date,
    sector_context: dict,
) -> str:
    """给 C 增加独立且保守的盘中板块扩散确认，不改写上游不可变快照。"""
    if not settings.PAPER_MAINLINE_LIVE_CONFIRM_ENABLED:
        return "主线扩散独立盘中确认开关未启用"
    snapshot_context = str(getattr(latest_run, "snapshot_context", "") or "")
    if (
        snapshot_context not in PAPER_MAINLINE_INTRADAY_CONTEXTS
        or getattr(latest_run, "reference_trade_date", None) != trade_date
    ):
        return "仅允许消费当日主线盘中不可变快照，禁止用收盘版或旧批次补单"
    if str(getattr(record, "rank_scope", "") or "") not in {"ranked", "recall_ranked"}:
        return "候选未进入正式Top12或Top30召回榜"

    factors = _json_loads_dict(getattr(record, "features_json", None))
    if not (
        factors.get("broad_rotation_member_setup")
        and factors.get("sector_catalyst_spread")
    ):
        return "治理快照缺少主线扩散成员与板块催化证据"
    try:
        probability = _promotion_snapshot_probability(record)
    except ProbabilityContractError as exc:
        return f"冻结生产概率合同无效：{exc}"
    if probability < settings.PAPER_MAINLINE_LIVE_CONFIRM_MIN_PROBABILITY:
        return (
            f"首板概率{probability:.3f}低于独立确认下限"
            f"{settings.PAPER_MAINLINE_LIVE_CONFIRM_MIN_PROBABILITY:.3f}"
        )
    strict_count = int(factors.get("strict_confirmation_count") or 0)
    if strict_count < settings.PAPER_MAINLINE_LIVE_CONFIRM_MIN_STRICT_CONFIRMATIONS:
        return (
            f"结构确认{strict_count}项，低于"
            f"{settings.PAPER_MAINLINE_LIVE_CONFIRM_MIN_STRICT_CONFIRMATIONS}项"
        )
    frozen_sector_strength = _to_float(factors.get("sector_strength_score")) or 0.0
    if (
        frozen_sector_strength < settings.PAPER_MAINLINE_LIVE_CONFIRM_MIN_SECTOR_STRENGTH
        and not factors.get("broad_rotation_cluster_setup")
    ):
        return "09:35快照的主线强度不足且未形成板块簇"

    if not sector_context:
        return "缺少快照所指同一板块的实时数据"
    if str(sector_context.get("sector_trade_date") or "") != trade_date.isoformat():
        return "同一板块实时数据不是当日快照"
    current_strength = _to_float(sector_context.get("sector_strength")) or 0.0
    current_change = _to_float(sector_context.get("sector_change_pct")) or 0.0
    current_flow = _to_float(sector_context.get("sector_fund_flow")) or 0.0
    current_limit_ups = int(sector_context.get("sector_limit_up_count") or 0)
    if current_strength < settings.PAPER_MAINLINE_LIVE_CONFIRM_MIN_SECTOR_STRENGTH:
        return (
            f"板块实时强度{current_strength:.0f}低于"
            f"{settings.PAPER_MAINLINE_LIVE_CONFIRM_MIN_SECTOR_STRENGTH:.0f}"
        )
    if current_change <= 0 or current_flow <= 0:
        return "板块尚未同时完成上涨与资金净流入确认"
    if current_limit_ups < settings.PAPER_MAINLINE_LIVE_CONFIRM_MIN_SECTOR_LIMIT_UP_COUNT:
        return (
            f"板块实时涨停{current_limit_ups}家，低于扩散确认所需"
            f"{settings.PAPER_MAINLINE_LIVE_CONFIRM_MIN_SECTOR_LIMIT_UP_COUNT}家"
        )
    return ""


async def _promotion_route_buy_candidates(
    db: AsyncSession,
    *,
    limit: int,
    trade_date: date,
    account_name: str,
    diagnostics: Optional[list[dict]] = None,
    now: Optional[datetime] = None,
    only_code: str | None = None,
) -> tuple[list[dict], list[str]]:
    """从决策时点可见的最新治理批次读取B/C/D候选，不向旧批次回退。"""
    cfg = PAPER_PROMOTION_ACCOUNTS.get(account_name)
    if not cfg:
        return [], [f"未知晋级预测账户: {account_name}"]
    route = cfg["route"]
    target_board = cfg["target_board"]
    label = cfg["label"]
    notes: list[str] = []
    if not cfg["enabled"]() and not experiment_active(account_name, at=trade_date):
        return [], [f"策略{label}开关未启用"]

    from app.api.v1.promotion import PROMOTION_MODEL_VERSION
    from app.models.promotion import PromotionPredictionRun, PromotionPredictionSnapshot

    # 15:10/20:00 只消费上一交易日收盘快照；B/D只用09:25/09:35开盘确认，
    # C额外消费10:00/10:30/13:05主线刷新。先锁定该策略允许的最新批次，
    # 再查具体赛道，确保质量失败或该批次无候选时都不会回退到更旧批次。
    previous_trade_date = await trade_calendar.previous_trade_day(trade_date)
    allowed_intraday_contexts = (
        PAPER_MAINLINE_ALLOWED_CONTEXTS
        if account_name == PAPER_ACCOUNT_MAINLINE
        else PAPER_OPEN_CONFIRM_CONTEXTS
    )
    latest_run = (
        await db.execute(
            select(PromotionPredictionRun)
            .where(
                PromotionPredictionRun.snapshot_source == "schedule",
                PromotionPredictionRun.model_version == PROMOTION_MODEL_VERSION,
                or_(
                    and_(
                        PromotionPredictionRun.snapshot_context.in_(("promotion_1510", "promotion_2000")),
                        PromotionPredictionRun.reference_trade_date == previous_trade_date,
                    ),
                    and_(
                        PromotionPredictionRun.snapshot_context.in_(allowed_intraday_contexts),
                        PromotionPredictionRun.reference_trade_date == trade_date,
                    ),
                ),
            )
            .order_by(desc(PromotionPredictionRun.as_of_at), desc(PromotionPredictionRun.id))
            .limit(1)
        )
    ).scalar_one_or_none()
    if latest_run is None:
        return [], [f"{label}赛道暂无今日允许消费的当前模型不可变正式批次"]
    if latest_run.status != "completed":
        reason = f"{label}最新正式批次{latest_run.run_key}状态为{latest_run.status}，禁止回退旧批次"
        if diagnostics is not None:
            metadata = _json_loads_dict(latest_run.metadata_json)
            persistence = metadata.get("persistence")
            persistence = persistence if isinstance(persistence, dict) else {}
            diagnostics.append({
                "reason": reason, "stage_code": "data_gate",
                "reason_code": "prediction_batch_blocked",
                "candidate": {
                    "run_key": latest_run.run_key, "route": route,
                    "run_status": latest_run.status,
                    "persistence_status": persistence.get("status"),
                    "input_count": persistence.get("input_count"),
                    "recordable_count": persistence.get("recordable_count"),
                    "prepared_count": persistence.get("prepared_count"),
                },
            })
        return [], [reason]
    # 必须先锁定最新批次再验时点；不能过滤掉污染/未完成批次后捞旧候选。
    # 本轮等待真实后续报价，而不是把更晚生成的预测配上旧轮次价格。
    decision_at = now or _paper_now()
    round_context = _quote_round_context()
    quote_as_of = round_context.get("as_of_at")
    clocks = {
        "as_of_at": latest_run.as_of_at,
        "created_at": latest_run.created_at,
        "completed_at": latest_run.completed_at,
    }
    clock_error = ""
    cutoff = decision_at
    if not isinstance(decision_at, datetime) or decision_at.tzinfo is not None:
        clock_error = "决策时点缺失或不是本地无时区时间"
    elif round_context.get("round_id"):
        if (
            not isinstance(quote_as_of, datetime)
            or quote_as_of.tzinfo is not None
            or quote_as_of.date() != trade_date
        ):
            clock_error = "行情轮次缺少当日有效as_of时点"
        else:
            cutoff = min(decision_at, quote_as_of)
    if not clock_error:
        if any(not isinstance(value, datetime) or value.tzinfo is not None for value in clocks.values()):
            clock_error = "批次缺少可验证的生成/完成时点"
        elif not (clocks["as_of_at"] <= clocks["created_at"] <= clocks["completed_at"] <= cutoff):
            clock_error = "批次生成/完成时点倒序或晚于本轮可见截止"
    if clock_error:
        reason = f"{label}最新正式批次{latest_run.run_key}时点合同不满足：{clock_error}，禁止回退旧批次"
        if diagnostics is not None:
            diagnostics.append({
                "reason": reason, "stage_code": "data_gate",
                "reason_code": "prediction_not_visible",
                "candidate": {
                    "run_key": latest_run.run_key, "route": route,
                    "decision_at": decision_at.isoformat() if isinstance(decision_at, datetime) else None,
                    "visible_cutoff": cutoff.isoformat() if isinstance(cutoff, datetime) else None,
                    **{key: value.isoformat() if isinstance(value, datetime) else None
                       for key, value in clocks.items()},
                },
            })
        return [], [reason]

    metadata = _json_loads_dict(latest_run.metadata_json)
    quality_gate = metadata.get("quality_gate") if isinstance(metadata.get("quality_gate"), dict) else {}
    from app.promotion.route_contract import persisted_route_gate
    route_evidence = persisted_route_gate(quality_gate, route)
    if route_evidence["gate_passed"] is not True:
        blocking_datasets = ",".join(route_evidence["blocking_datasets"])
        suffix = f"（阻断数据集: {blocking_datasets}）" if blocking_datasets else ""
        if route_evidence["gate_issue"]:
            suffix += f"（质量契约: {route_evidence['gate_issue']}）"
        if diagnostics is not None:
            diagnostics.append({
                "reason": f"{label}批次{latest_run.run_key}数据质量闸门未通过{suffix}",
                "stage_code": "data_gate", "reason_code": "candidate_data_missing",
                "candidate": {
                    "run_key": latest_run.run_key, "route": route,
                    "blocking_datasets": route_evidence["blocking_datasets"],
                    "route_contract_status": route_evidence["contract_status"],
                    "recorded_route_contract_version": route_evidence["recorded_contract_version"],
                    "gate_issue": route_evidence["gate_issue"],
                    # Frozen bad/missing evidence cannot recover in-place.
                    "recoverable": False, "requires_new_formal_attempt": True,
                },
            })
        return [], [
            f"{label}最新正式批次{latest_run.run_key}的{route}路线质量闸门未通过{suffix}，禁止回退旧批次"
        ]

    signal_date = (
        trade_date
        if latest_run.snapshot_context in allowed_intraday_contexts
        else previous_trade_date
    )
    min_probability = float(cfg["min_probability"]())
    enforce_probability_floor = bool(cfg.get("enforce_probability_floor", True))
    snapshot_filters = [
        PromotionPredictionSnapshot.run_id == latest_run.id,
        PromotionPredictionSnapshot.target_board == target_board,
        PromotionPredictionSnapshot.candidate_route == route,
        PromotionPredictionSnapshot.prediction_trade_date == signal_date,
        PromotionPredictionSnapshot.trade_gate_passed.is_(True),
        PromotionPredictionSnapshot.watch_only.is_(False),
    ]
    if account_name == PAPER_ACCOUNT_MAINLINE:
        # 当前首板模型把“结构可执行”与“主动盘口确认”拆开，而主线路线没有
        # 独立的上游主动确认字段。仅给当日主线盘中正式榜/召回榜保留下游
        # 同板块实时扩散确认机会；B/D仍必须原样消费 actionable=true。
        snapshot_filters.append(or_(
            PromotionPredictionSnapshot.actionable.is_(True),
            PromotionPredictionSnapshot.rank_scope.in_(("ranked", "recall_ranked")),
        ))
    else:
        snapshot_filters.append(PromotionPredictionSnapshot.actionable.is_(True))
    # Validate the entire selected route pool before SQL eligibility/probability
    # filters can hide bad evidence. Never use old columns to prefilter new values.
    probability_pool = list((await db.scalars(select(PromotionPredictionSnapshot).where(
        PromotionPredictionSnapshot.run_id == latest_run.id,
        PromotionPredictionSnapshot.target_board == target_board,
        PromotionPredictionSnapshot.candidate_route == route,
        PromotionPredictionSnapshot.prediction_trade_date == signal_date,
    ))).all())
    probabilities = {}
    for row in probability_pool:
        try:
            probabilities[row.id] = _promotion_snapshot_probability(row)
        except ProbabilityContractError as exc:
            reason = f"{label}最新正式批次冻结生产概率合同无效，禁止回退旧概率或旧批次：{exc}"
            if diagnostics is not None:
                diagnostics.append({
                    "reason": reason,
                    "stage_code": "data_gate",
                    "reason_code": "probability_contract_invalid",
                    "candidate": {"run_key": latest_run.run_key, "route": route,
                                  "code": row.code, "route_pool": len(probability_pool)},
                })
            return [], [reason]
    records = (
        await db.execute(
            select(PromotionPredictionSnapshot)
            .where(*snapshot_filters)
            .order_by(
                PromotionPredictionSnapshot.rank_position,
                PromotionPredictionSnapshot.id,
            )
        )
    ).scalars().all()
    records = [
        row for row in records
        if not enforce_probability_floor or probabilities[row.id] >= min_probability
    ]
    records.sort(key=lambda row: (
        -probabilities[row.id], row.rank_position if row.rank_position is not None else -1, row.id,
    ))
    if not records:
        if account_name == PAPER_ACCOUNT_MAINLINE:
            eligibility = "治理快照可执行标志或当日主线盘中正式榜/召回榜入口"
        else:
            eligibility = (
                f"单候选可执行标志与概率≥{min_probability:.2f}"
                if enforce_probability_floor
                else "治理快照单候选可执行标志"
            )
        if diagnostics is not None:
            pool = list((await db.scalars(select(PromotionPredictionSnapshot).where(
                PromotionPredictionSnapshot.run_id == latest_run.id,
                PromotionPredictionSnapshot.target_board == target_board,
                PromotionPredictionSnapshot.candidate_route == route,
                PromotionPredictionSnapshot.prediction_trade_date == signal_date,
            ))).all())
            gate_rows = [row for row in pool if row.trade_gate_passed is True and row.watch_only is False]
            ranked_rows = [row for row in gate_rows if row.rank_scope in ("ranked", "recall_ranked")]
            actionable_rows = [row for row in gate_rows if row.actionable is True]
            counts = {
                "route_pool": len(pool), "trade_gate_nonwatch": len(gate_rows),
                "actionable": len(actionable_rows), "ranked_or_recall": len(ranked_rows),
                "probability_pass": sum(
                    probabilities[row.id] >= min_probability
                    for row in gate_rows
                ) if enforce_probability_floor else None,
                "executable_intersection": 0,
            }
            rank_note = ""
            if account_name == PAPER_ACCOUNT_MAINLINE:
                rank_eligible_rows = [
                    row for row in pool
                    if _json_loads_dict(row.features_json).get("prediction_rank_eligible") is True
                ]
                counts.update({
                    "prediction_rank_eligible": len(rank_eligible_rows),
                    "rank_eligible_unranked": sum(
                        row.rank_scope == "pool_unranked" for row in rank_eligible_rows
                    ),
                    "trade_eligible_unranked": sum(
                        row.rank_scope == "pool_unranked" for row in gate_rows
                    ),
                })
                rank_note = (
                    f"具备预测排名资格{counts['prediction_rank_eligible']}，"
                    f"其中未进入全局正式/召回榜{counts['rank_eligible_unranked']}；"
                    "排名资格不等于入榜，入榜仍需原有实时确认；"
                )
            diagnostics.append({
                "reason": (
                    f"{label}执行条件交集为0：路线池{len(pool)}，交易资格非观察{len(gate_rows)}，"
                    f"可执行{len(actionable_rows)}，正式/召回排名{len(ranked_rows)}；"
                    f"{rank_note}仍需{eligibility}，不将观察池升级买单"
                ),
                "stage_code": "strategy_filter", "reason_code": "candidate_contract_empty",
                "metric_value": 0,
                "candidate": {"run_key": latest_run.run_key, "route": route,
                              "snapshot_context": latest_run.snapshot_context,
                              "funnel": counts, "min_probability": min_probability,
                              "enforce_probability_floor": enforce_probability_floor},
            })
        return [], [f"{label}最新正式批次无满足{eligibility}的候选"]

    min_confirm = cfg["min_confirm_change"]()
    max_confirm = cfg["max_confirm_change"]()
    stop_loss_pct = _promotion_stop_loss_pct(account_name)
    take_profit_pct = _promotion_take_profit_pct(account_name)

    candidates: list[dict] = []
    rejected = 0
    mainline_rejections: list[str] = []
    for record in records:
        if len(candidates) >= limit:
            break
        code = str(record.code or "").strip()
        if not code or (only_code is not None and code != only_code):
            continue
        spot = await _spot_by_code(db, code)
        if not spot or not spot.price or float(spot.price) <= 0:
            rejected += 1
            continue
        price = float(spot.price)
        prev_close = float(getattr(spot, "prev_close", 0) or 0)
        if prev_close <= 0:
            rejected += 1
            continue
        change_pct = _to_float(getattr(spot, "change_pct", None))
        if change_pct is None:
            rejected += 1
            continue
        # 盘中确认: 现价涨幅下限(不低开/要求高开)与上限(不追高)
        if change_pct < min_confirm:
            rejected += 1
            continue
        if change_pct > max_confirm:
            rejected += 1
            continue
        # 涨停/一字板不追 (用 limit_up 或涨幅判断)
        limit_up_price = float(getattr(spot, "limit_up", 0) or 0)
        if limit_up_price > 0 and price >= limit_up_price * 0.998:
            rejected += 1
            continue
        # 量比健康 (有承接, 不缩量)
        volume_ratio = _to_float(getattr(spot, "volume_ratio", None))
        if volume_ratio is None or volume_ratio <= 0 or volume_ratio < 0.6:
            rejected += 1
            continue
        # ST/退市过滤 — tag 标记 或 stock_blacklist 命中即拦截 (2026-08-31 修复 600002 类静默通过)
        tag = (
            await db.execute(select(StockTag).where(StockTag.code == code))
        ).scalar_one_or_none()
        blocked_reason = (
            await db.execute(
                select(StockBlacklist).where(
                    StockBlacklist.code == code,
                    StockBlacklist.reason.in_(["st", "delisting", "suspended"]),
                    _active_blacklist_clause(trade_date),
                )
            )
        ).scalar_one_or_none()
        if blocked_reason:
            rejected += 1
            notes.append(f"{code} 在黑名单({blocked_reason.reason})中, 拒绝候选")
            continue
        if (
            not stock_tagger.is_tradeable(code)
            or (tag is not None and tag.board_tag != "tradeable")
            or (tag and (tag.is_st or tag.is_suspended or tag.is_delisting))
        ):
            rejected += 1
            notes.append(f"{code} 非主板可交易标的或标记为 ST/退市/停牌, 拒绝候选")
            continue

        conditional_mainline_confirmation = False
        mainline_sector_context: dict = {}
        if account_name == PAPER_ACCOUNT_MAINLINE and record.actionable is not True:
            factors = _json_loads_dict(record.features_json)
            mainline_sector_context = await _promotion_mainline_live_sector_context(
                db,
                code=code,
                factors=factors,
            )
            mainline_reject_reason = _promotion_mainline_live_confirm_reject_reason(
                record,
                latest_run,
                trade_date=trade_date,
                sector_context=mainline_sector_context,
            )
            if mainline_reject_reason:
                rejected += 1
                if len(mainline_rejections) < 3:
                    mainline_rejections.append(
                        f"{code} {record.name or spot.name or code}：{mainline_reject_reason}"
                    )
                continue
            conditional_mainline_confirmation = True

        probability = probabilities[record.id]
        frozen_news_evidence = _json_loads_dict(record.features_json).get("news_evidence")
        stop_loss = price * (1 - stop_loss_pct / 100)
        entry_condition = f"盘中确认: 涨幅{min_confirm:.1f}%~{max_confirm:.1f}%, 量比健康"
        if conditional_mainline_confirmation:
            entry_condition += (
                f"；{latest_run.snapshot_context.replace('promotion_', '')}主线榜完成"
                "同板块上涨/资金/涨停扩散二次确认"
            )
        candidates.append({
            "code": code,
            "name": str(record.name or spot.name or code),
            "_source": f"promotion_{account_name}",
            "signal_source": label,
            "total_score": min(99.0, probability * 100),
            "probability": probability,
            "price": price,
            "change_pct": change_pct,
            "volume_ratio": volume_ratio,
            "stop_loss_price": round(stop_loss, 2),
            "stop_loss_pct": stop_loss_pct,
            "take_profit_pct": take_profit_pct,
            "signal_date": signal_date.isoformat(),
            "prediction_run_id": latest_run.id,
            "prediction_snapshot_id": record.id,
            # Frozen provenance only; never supplement from current news.
            "news_evidence": deepcopy(frozen_news_evidence) if isinstance(frozen_news_evidence, dict) else {},
            "prediction_run_key": latest_run.run_key,
            "snapshot_context": latest_run.snapshot_context,
            "trade_gate_passed": True,
            "snapshot_actionable": bool(record.actionable),
            "conditional_mainline_confirmation": conditional_mainline_confirmation,
            "execution_confirmation": bool(record.actionable or conditional_mainline_confirmation),
            "actionable": bool(record.actionable or conditional_mainline_confirmation),
            "watch_only": False,
            "strategy_label": f"{label}（基准策略）",
            "entry_condition": entry_condition,
            **mainline_sector_context,
        })

    notes.extend(mainline_rejections)
    if rejected:
        notes.append(f"已过滤{rejected}只不满足盘中确认条件(行情缺失/低开/追高/一字/缩量/ST)的{label}候选")
    if not candidates:
        notes.append(f"{label}候选均未通过盘中确认，等待下一次轮询")
    return candidates[:limit], notes


def _promotion_stop_loss_pct(account_name: str) -> float:
    """晋级预测各账户的止损% (供候选生成与卖出参数共用)."""
    return {
        PAPER_ACCOUNT_PROMOTION: settings.PAPER_PROMOTION_STOP_LOSS_PCT,
        PAPER_ACCOUNT_MAINLINE: settings.PAPER_MAINLINE_STOP_LOSS_PCT,
        PAPER_ACCOUNT_AUCTION: settings.PAPER_AUCTION_STOP_LOSS_PCT,
    }.get(account_name, settings.PAPER_PROMOTION_STOP_LOSS_PCT)


def _promotion_take_profit_pct(account_name: str) -> float:
    """晋级预测各账户的止盈%."""
    return {
        PAPER_ACCOUNT_PROMOTION: settings.PAPER_PROMOTION_TAKE_PROFIT_PCT,
        PAPER_ACCOUNT_MAINLINE: settings.PAPER_MAINLINE_TAKE_PROFIT_PCT,
        PAPER_ACCOUNT_AUCTION: settings.PAPER_AUCTION_TAKE_PROFIT_PCT,
    }.get(account_name, settings.PAPER_PROMOTION_TAKE_PROFIT_PCT)


async def _promotion_second_board_buy_candidates(
    db: AsyncSession,
    *,
    limit: int,
    trade_date: date,
) -> tuple[list[dict], list[str]]:
    """策略B候选 (兼容旧调用): 晋级预测二板赛道."""
    return await _promotion_route_buy_candidates(
        db, limit=limit, trade_date=trade_date, account_name=PAPER_ACCOUNT_PROMOTION,
    )


async def _tenbagger_pullback_confirmed(
    db: AsyncSession,
    code: str,
    price: float,
) -> tuple[bool, str]:
    """十倍潜力回踩买入确认 (2026-08-31 重建).

    诊断发现: 十倍评分高分票往往已涨过一波, 买入后处于回调期 → 评分与未来收益负相关.
    修复: 只在"回调到位"时才买:
      1. 现价距 MA20 偏离 ≤ PAPER_TENBAGGER_PULLBACK_MA20_MAX_DIST_PCT (回踩MA20附近)
      2. 从 60日高点回撤 ≥ MIN_PCT 且 ≤ MAX_PCT (已回调但未破位)
      3. 近5日均量 / 前20日均量 ≤ PAPER_TENBAGGER_SHRINK_VOLUME_RATIO (缩量企稳)
    """
    k_rows = await _latest_kline_rows(db, code, limit=60)
    if len(k_rows) < 25:
        return False, "K线不足(需≥25根)无法确认回踩"
    closes = [_to_float(row.close) for row in k_rows]
    volumes = [_to_float(row.volume) for row in k_rows]
    closes = [v for v in closes if v is not None]
    volumes = [v for v in volumes if v is not None]
    if len(closes) < 20 or len(volumes) < 25:
        return False, "K线数据不足"

    ma20 = round(sum(closes[-20:]) / 20, 4)
    high_60d = max(closes[-60:]) if len(closes) >= 60 else max(closes)
    # 1. 回踩MA20
    ma20_dist = (price / ma20 - 1) * 100 if ma20 > 0 else 999
    if ma20_dist > settings.PAPER_TENBAGGER_PULLBACK_MA20_MAX_DIST_PCT:
        return False, f"现价距MA20偏离{ma20_dist:.1f}% > 上限{settings.PAPER_TENBAGGER_PULLBACK_MA20_MAX_DIST_PCT:.1f}%, 未回踩到位"
    if ma20_dist < -3.0:
        return False, f"现价已深跌破MA20({ma20_dist:.1f}%), 趋势转弱"
    # 2. 从60日高点回撤
    pullback = (price / high_60d - 1) * 100 if high_60d > 0 else 0
    if pullback > -settings.PAPER_TENBAGGER_PULLBACK_FROM_HIGH_MIN_PCT:
        return False, f"距60日高点仅回撤{abs(pullback):.1f}% < 要求{settings.PAPER_TENBAGGER_PULLBACK_FROM_HIGH_MIN_PCT:.1f}%, 尚未回调"
    if pullback < -settings.PAPER_TENBAGGER_PULLBACK_FROM_HIGH_MAX_PCT:
        return False, f"距60日高点已回撤{abs(pullback):.1f}% > 上限{settings.PAPER_TENBAGGER_PULLBACK_FROM_HIGH_MAX_PCT:.1f}%, 下跌趋势"
    # 3. 缩量企稳 (近5日均量 / 前20日均量)
    recent_vol = sum(volumes[-5:]) / 5 if len(volumes) >= 5 else 0
    prev_vol = sum(volumes[-25:-5]) / 20 if len(volumes) >= 25 else recent_vol
    vol_ratio = recent_vol / prev_vol if prev_vol > 0 else 999
    if vol_ratio > settings.PAPER_TENBAGGER_SHRINK_VOLUME_RATIO:
        return False, f"量能未萎缩(近5日/前20日均量={vol_ratio:.2f} > {settings.PAPER_TENBAGGER_SHRINK_VOLUME_RATIO:.2f}), 未企稳"
    return True, (
        f"回踩确认: MA20偏离{ma20_dist:.1f}% / 60日高点回撤{abs(pullback):.1f}% / "
        f"量比{vol_ratio:.2f}"
    )


def _highboard_policy(account_name: str) -> dict:
    """E/E2共享算法实现但不共享可调参数；单位保持封单亿元、仓位比例。"""
    is_e2 = account_name == PAPER_ACCOUNT_CHALLENGER_E
    prefix = "PAPER_CHALLENGER_E" if is_e2 else "PAPER_HIGHBOARD"
    values = {
        key: getattr(settings, f"{prefix}_{key.upper()}")
        for key in (
            "min_consecutive", "max_consecutive", "min_seal_amount", "max_break_count",
            "require_above_vwap", "max_pullback_from_high_pct", "limit_up_queue_enabled",
            "intraday_buy_start", "queue_cancel_time", "take_profit_pct", "stop_loss_pct",
            "max_hold_days",
        )
    }
    values["position_pct"] = (
        settings.PAPER_CHALLENGER_E_POSITION_PCT if is_e2 else settings.PAPER_TENBAGGER_POSITION_PCT
    )
    values["max_entry_change_pct"] = (
        settings.PAPER_CHALLENGER_E_MAX_ENTRY_CHANGE_PCT if is_e2
        else settings.PAPER_TENBAGGER_MAX_INTRADAY_CONFIRM_CHANGE_PCT
    )
    return values


async def _tenbagger_midline_candidates(
    db: AsyncSession,
    *,
    limit: int,
    trade_date: date,
    account_name: str = PAPER_ACCOUNT_TENBAGGER,
    diagnostics: Optional[list[dict]] = None,
    only_code: str | None = None,
) -> tuple[list[dict], list[str]]:
    """策略E候选: 连板高标接力 (2026-08-31 重建, 取代十倍评分).

    数据依据 (回放诊断):
      - 十倍评分模型5维与未来收益负相关(总分越高越亏), E回放全线亏损
      - 连板≥4 高标: 次日买入持有5日 +3.13% 胜率55.6% (唯一正期望信号)
      - 加-8%止损后期望提升到 +2.35% (截断-35%尾部)

    数据流: limit_up_pool (连板≥4) → 涨停质量过滤(封板资金/炸板) →
            次日盘中确认(非一字板可交易) → 买入.
    """
    notes: list[str] = []
    if not settings.PAPER_TENBAGGER_ENABLED and not experiment_active(account_name, at=trade_date):
        return [], ["策略E(连板高标接力)开关未启用"]
    is_e2 = account_name == PAPER_ACCOUNT_CHALLENGER_E
    cfg = _highboard_policy(account_name)
    max_entry_change = cfg["max_entry_change_pct"]

    from app.models.stock import LimitUpPool

    signal_date = await trade_calendar.previous_trade_day(trade_date)
    # 1. 只取上一交易日收盘确认的高标信号，下一交易日盘中执行。
    rows = (
        await db.execute(
            select(LimitUpPool)
            .where(
                LimitUpPool.trade_date == signal_date,
                LimitUpPool.consecutive_days >= cfg['min_consecutive'],
                LimitUpPool.consecutive_days <= cfg['max_consecutive'],
                LimitUpPool.quarantined.is_(False),
            )
            .order_by(desc(LimitUpPool.consecutive_days), desc(LimitUpPool.seal_amount))
        )
    ).scalars().all()

    if not rows:
        return [], [f"上一交易日{signal_date}无连板{cfg['min_consecutive']}-{cfg['max_consecutive']}板高标候选"]

    candidates: list[dict] = []
    rejected = 0
    for item in rows:
        code = str(item.code or "").strip()
        if not code or (only_code is not None and code != only_code):
            continue
        def reject(reason_code: str, reason: str, metric=None, threshold=None, *, data=False):
            if diagnostics is not None:
                diagnostics.append({
                    "code": code, "name": str(item.name or code),
                    "reason": reason, "reason_code": reason_code,
                    "stage_code": "data_gate" if data else "strategy_filter",
                    "metric_value": metric, "threshold_value": threshold,
                    "candidate": {"signal_date": signal_date.isoformat(),
                                  "recoverable": data, "entry_account": account_name},
                })

        # 2. 涨停质量: 封板资金 ≥ 阈值, 炸板次数 ≤ 上限
        seal_amount = _to_float(item.seal_amount)
        if seal_amount is None:
            reject("seal_amount_missing", "昨日封单数据缺失，不能当作0或直接通过", data=True)
            rejected += 1
            continue
        if seal_amount < cfg['min_seal_amount'] * 1e8:
            reject("seal_amount_below_min",
                   f"昨日封单{seal_amount:.2f}元低于门槛{cfg['min_seal_amount'] * 1e8:.2f}元",
                   seal_amount, cfg['min_seal_amount'] * 1e8)
            rejected += 1
            continue
        break_count = int(item.break_count or 0)
        if break_count > cfg['max_break_count']:
            reject("break_count_above_max", f"昨日炸板{break_count}次超过策略上限",
                   break_count, cfg['max_break_count'])
            rejected += 1
            continue
        # 3. 实时 spot 确认：开板时直接成交；曾开板后回封可提交涨停排队。
        # 真一字板仍不纳入，避免改变当前高标策略的已验证样本口径。
        spot = await _spot_by_code(db, code)
        if not spot or not spot.price or float(spot.price) <= 0:
            reject("quote_missing", "本轮高标行情缺失或无效，等待新鲜报价", data=True)
            rejected += 1
            continue
        price = float(spot.price)
        change_pct = _to_float(getattr(spot, "change_pct", None))
        if (
            change_pct is None
            or change_pct
            > max_entry_change
        ):
            reject("change_missing" if change_pct is None else "entry_change_above_max",
                   "本轮涨幅缺失" if change_pct is None else f"当前涨幅{change_pct:.2f}%超过入口上限{max_entry_change:.2f}%",
                   change_pct, max_entry_change, data=change_pct is None)
            rejected += 1
            continue
        avg_price = _to_float(getattr(spot, "avg_price", None))
        if (
            cfg['require_above_vwap']
            and (avg_price is None or avg_price <= 0 or price < avg_price)
        ):
            missing_vwap = avg_price is None or avg_price <= 0
            reject("vwap_missing" if missing_vwap else "below_vwap",
                   "本轮VWAP缺失或无效" if missing_vwap else f"现价{price:.2f}低于VWAP{avg_price:.2f}",
                   price, avg_price, data=missing_vwap)
            rejected += 1
            continue
        high_price = _to_float(getattr(spot, "high", None))
        pullback_from_high_pct = (
            max(0.0, (high_price - price) / high_price * 100.0)
            if high_price is not None and high_price > 0
            else None
        )
        if (
            pullback_from_high_pct is None
            or pullback_from_high_pct
            > cfg['max_pullback_from_high_pct']
        ):
            reject("high_missing" if pullback_from_high_pct is None else "high_pullback_above_max",
                   "本轮日内高点缺失" if pullback_from_high_pct is None else f"高点回撤{pullback_from_high_pct:.2f}%超过入口上限",
                   pullback_from_high_pct, cfg['max_pullback_from_high_pct'],
                   data=pullback_from_high_pct is None)
            rejected += 1
            continue
        limit_up_price = float(getattr(spot, "limit_up", 0) or 0)
        if limit_up_price <= 0:
            reject("limit_price_missing", "本轮涨停价缺失，无法校验价格边界", data=True)
            rejected += 1
            continue
        limit_up_queue = False
        currently_at_limit = bool(
            limit_up_price > 0 and round(price, 2) >= round(limit_up_price, 2)
        )
        if currently_at_limit:
            ask1_price = _to_float(getattr(spot, "ask1_price", None))
            has_marketable_offer = bool(
                ask1_price
                and ask1_price > 0
                and ask1_price <= limit_up_price + 0.005
            )
            if not has_marketable_offer:
                open_price = _to_float(getattr(spot, "open", None))
                low_price = _to_float(getattr(spot, "low", None))
                traded_below_limit = any(
                    value is not None
                    and value > 0
                    and round(value, 2) < round(limit_up_price, 2)
                    for value in (open_price, low_price)
                )
                if not (
                    cfg['limit_up_queue_enabled']
                    and traded_below_limit
                    and _is_limit_up_queue_quote(spot)
                ):
                    reject("limit_queue_ineligible", "无可成交卖盘且不满足曾开板回封排队条件，禁止假成交")
                    rejected += 1
                    continue
                limit_up_queue = True
        # 4. ST/停牌/退市过滤 — tag 标记 或 stock_blacklist 命中即拦截 (2026-08-31 修复)
        tag = (
            await db.execute(select(StockTag).where(StockTag.code == code))
        ).scalar_one_or_none()
        blocked_reason = (
            await db.execute(
                select(StockBlacklist).where(
                    StockBlacklist.code == code,
                    StockBlacklist.reason.in_(["st", "delisting", "suspended"]),
                    _active_blacklist_clause(trade_date),
                )
            )
        ).scalar_one_or_none()
        if blocked_reason:
            reject("security_ineligible", f"黑名单资格拦截：{blocked_reason.reason}")
            rejected += 1
            notes.append(f"{code} 在黑名单({blocked_reason.reason})中, 拒绝候选")
            continue
        if (
            not stock_tagger.is_tradeable(code)
            or (tag is not None and tag.board_tag != "tradeable")
            or (tag and (tag.is_st or tag.is_suspended or tag.is_delisting))
        ):
            reject("security_ineligible", "非主板可交易范围或ST/停牌/退市标记")
            rejected += 1
            continue

        consecutive = int(item.consecutive_days or 0)
        seal_score = min(10.0, max(0.0, seal_amount / 1e8))
        vwap_premium_pct = (
            max(0.0, (price / avg_price - 1.0) * 100.0)
            if avg_price is not None and avg_price > 0
            else 0.0
        )
        execution_quality_score = min(
            99.0,
            55.0
            + consecutive * 5.0
            + seal_score
            + min(5.0, vwap_premium_pct * 2.0)
            - break_count * 3.0
            - min(5.0, (pullback_from_high_pct or 0.0) * 2.0),
        )
        stop_loss_pct = cfg['stop_loss_pct']
        stop_loss = price * (1 - stop_loss_pct / 100)
        candidates.append({
            "code": code,
            "name": str(item.name or spot.name or code),
            "_source": "tenbagger_midline",
            "signal_source": "连板高标接力",
            "total_score": round(execution_quality_score, 2),
            "consecutive_days": consecutive,
            "seal_amount": round(seal_amount / 1e8, 2),
            "break_count": break_count,
            "price": price,
            "change_pct": change_pct,
            "avg_price": avg_price,
            "high_price": high_price,
            "vwap_premium_pct": round(vwap_premium_pct, 2),
            "pullback_from_high_pct": round(pullback_from_high_pct or 0.0, 2),
            "limit_up_queue": limit_up_queue,
            "queue_ahead_hands": (
                int(getattr(spot, "bid1_volume", 0) or 0)
                if limit_up_queue
                else 0
            ),
            "stop_loss_price": round(stop_loss, 2),
            "stop_loss_pct": stop_loss_pct,
            "take_profit_pct": cfg['take_profit_pct'],
            "max_hold_days": cfg['max_hold_days'],
            "signal_date": signal_date.isoformat(),
            "strategy_label": "E2高标强势/回封实验" if is_e2 else "E高标低位接力实验",
            "entry_variant": "e2_strong_reseal" if is_e2 else "e_low_entry",
            "max_entry_change_pct": max_entry_change,
            "entry_condition": (
                f"{signal_date}连板{consecutive}板, 封板资金{seal_amount/1e8:.1f}亿, "
                f"炸板{break_count}次；"
                + (
                    "次日曾开板后回封，按涨停价排队等待真实成交证据"
                    if limit_up_queue
                    else "次日已开板可交易"
                )
            ),
            "limit_up_reason": str(item.limit_up_reason or ""),
        })

    candidates.sort(
        key=lambda item: (
            _to_float(item.get("total_score")) or 0.0,
            int(item.get("consecutive_days") or 0),
            _to_float(item.get("seal_amount")) or 0.0,
            -int(item.get("break_count") or 0),
            str(item.get("code") or ""),
        ),
        reverse=True,
    )
    if rejected:
        notes.append(
            f"已过滤{rejected}只(封板质量/涨幅上限/VWAP/高点回撤/盘口/ST)不达标的高标候选"
        )
    if not candidates:
        notes.append("高标候选均未通过持续强度确认，等待下一轮或下一交易日")
    return candidates[:limit], notes


def _reversal_kline_pattern(
    klines: list[StockKline], signal_date: date, cfg: dict,
    *, diagnostics: Optional[list[dict]] = None,
) -> Optional[dict]:
    """保持原形态/阈值；可选记录首个拒绝节点，不认证历史数据PIT。"""
    def reject(reason_code: str, reason: str, metric=None, threshold=None, *, data=False):
        if diagnostics is not None:
            diagnostics.append({
                "reason_code": reason_code, "reason": reason,
                "stage_code": "data_gate" if data else "strategy_filter",
                "metric_value": metric, "threshold_value": threshold,
            })
        return None

    hist = sorted(
        (row for row in klines if row.trade_date <= signal_date),
        key=lambda row: row.trade_date,
    )
    if len(hist) < 6:
        return reject("reversal_history_insufficient", "反包形态至少需要6根已收盘历史K线",
                      len(hist), 6, data=True)
    if hist[-1].trade_date != signal_date:
        return reject("reversal_signal_bar_missing", "缺少上一交易日信号K线，不回退旧信号", data=True)

    sealed: list[bool] = []
    limit_prices: list[float] = []
    for index, row in enumerate(hist):
        code = str(row.code or "")
        rule = price_limit_rule(
            code,
            trade_date=row.trade_date,
            adjusted_bar=True,
        )
        prev_close = _to_float(getattr(row, "prev_close", None))
        if (prev_close is None or prev_close <= 0) and index > 0:
            prev_close = _to_float(getattr(hist[index - 1], "close", None))
        limit_price = (
            round(prev_close * (1 + rule.nominal_limit_pct / 100.0), 2)
            if prev_close and prev_close > 0
            else 0.0
        )
        close = _to_float(getattr(row, "close", None)) or 0.0
        limit_prices.append(limit_price)
        sealed.append(
            bool(limit_price > 0 and close >= limit_price * 0.995)
            or is_limit_up_change(
                code,
                _to_float(getattr(row, "change_pct", None)),
                trade_date=row.trade_date,
                adjusted_bar=True,
            )
        )

    signal_index = len(hist) - 1
    if not sealed[signal_index]:
        return reject("reversal_signal_not_sealed", "上一交易日未按现有价格规则封板，尚非已收盘反包信号")
    if sealed[signal_index - 1]:
        return reject("reversal_no_break", "信号日前一日仍封板，属于延续而非断板反包")

    last_sealed_index = next(
        (index for index in range(signal_index - 1, -1, -1) if sealed[index]),
        -1,
    )
    if last_sealed_index < 0:
        return reject("reversal_prior_limit_missing", "已读历史窗没有更早封板，无法识别断板前连板")
    consecutive = 1
    cursor = last_sealed_index
    while cursor > 0 and sealed[cursor - 1]:
        consecutive += 1
        cursor -= 1
    # 断板交易日数不包含最后涨停日和当前反包日。
    gap_days = signal_index - last_sealed_index - 1
    if consecutive < int(cfg["min_consecutive"]):
        return reject("reversal_prior_boards_below_min", "断板前连板数不足",
                      consecutive, int(cfg["min_consecutive"]))
    if not 1 <= gap_days <= int(cfg["max_gap_days"]):
        return reject("reversal_gap_outside_window", "断板交易日数不在1至策略上限内",
                      gap_days, int(cfg["max_gap_days"]))

    anchor_close = _to_float(getattr(hist[last_sealed_index], "close", None))
    break_window = hist[last_sealed_index + 1:signal_index]
    break_lows = [
        value
        for row in break_window
        if (value := _to_float(getattr(row, "low", None))) is not None and value > 0
    ]
    if anchor_close is None or anchor_close <= 0 or not break_lows:
        return reject("reversal_dip_evidence_missing", "连板锚点或断板窗口低点缺失", data=True)
    window_dip_pct = (min(break_lows) / anchor_close - 1.0) * 100.0
    if window_dip_pct > float(cfg["min_dip_pct"]):
        return reject("reversal_dip_not_deep_enough", "断板窗口回撤未达到策略要求",
                      window_dip_pct, float(cfg["min_dip_pct"]))

    prev_day = hist[signal_index - 1]
    prev_day_change = _to_float(getattr(prev_day, "change_pct", None))
    if prev_day_change is None:
        prev_close = _to_float(getattr(prev_day, "prev_close", None))
        prev_day_close = _to_float(getattr(prev_day, "close", None))
        prev_day_change = (
            (prev_day_close / prev_close - 1.0) * 100.0
            if prev_close and prev_close > 0 and prev_day_close is not None
            else 0.0
        )

    prior_volumes = [
        _to_float(getattr(row, "volume", None))
        for row in hist[signal_index - 5:signal_index]
    ]
    signal_volume = _to_float(getattr(hist[signal_index], "volume", None))
    if signal_volume is None or signal_volume <= 0 or any(
        value is None or value <= 0 for value in prior_volumes
    ):
        return reject("reversal_volume_missing", "信号日或前5根历史成交量缺失/无效", data=True)
    avg5_volume = sum(prior_volumes) / 5
    volume_ratio = signal_volume / avg5_volume
    if volume_ratio < float(cfg["min_vol_ratio"]):
        return reject("reversal_volume_below_min", "已收盘信号日量比不足",
                      volume_ratio, float(cfg["min_vol_ratio"]))

    signal_open = _to_float(getattr(hist[signal_index], "open", None))
    signal_limit = limit_prices[signal_index]
    if signal_open is None or signal_open <= 0:
        return reject("reversal_signal_open_missing", "已收盘信号日开盘价缺失/无效", data=True)
    if signal_limit > 0 and signal_open >= signal_limit * 0.998:
        return reject("reversal_signal_open_at_limit", "信号日开盘已接近涨停价，不符合原非一字开盘条件",
                      signal_open, signal_limit * 0.998)

    return {
        "consec_before": consecutive,
        "gap_days": gap_days,
        "dip_min": round(window_dip_pct, 2),
        "window_dip_pct": round(window_dip_pct, 2),
        "prev_day_chg": round(prev_day_change, 2),
        "vol_ratio": round(volume_ratio, 2),
    }


async def _reversal_pullback_candidates(
    db: AsyncSession,
    limit: int,
    trade_date: date,
    only_code: str | None = None,
    diagnostics: Optional[list[dict]] = None,
) -> tuple[list[dict], list[str]]:
    """策略F候选: 断板反包 (2026-08-31 晚新增).

    数据依据 (全市场 2018-2026 回放, scripts/replay_break_reversal.py):
      - 2026-09-01 修正断板交易日 off-by-one、T+1、右删失和主板交易域后，
        宽松及严格网格均为负收益；严格子集(连板≥3、断板≤3、窗口回撤≤-5%、
        放量≥1.5x)共397个信号、388笔可评估，12/8/3均收-1.01%、胜率41.5%。
      - 个别成功反包只能证明形态存在，不能推翻全样本基准率；F1 默认暂停自动候选，
        保留本函数仅供显式研究开关和历史兼容，盘中短调整形态进入 F2 影子路线。

    若研究开关显式启用，数据流仍为: 上一交易日涨停池 → 收盘K线确认 →
    下一交易日实时行情确认非涨停开盘后才可形成候选。
    """
    notes: list[str] = []
    if not settings.PAPER_REVERSAL_ENABLED and not experiment_active(PAPER_ACCOUNT_REVERSAL, at=trade_date):
        return [], ["策略F(断板反包)开关未启用"]

    from app.models.stock import LimitUpPool

    cfg = {
        "min_consecutive": settings.PAPER_REVERSAL_MIN_CONSECUTIVE,
        "max_gap_days": settings.PAPER_REVERSAL_MAX_GAP_DAYS,
        "min_dip_pct": settings.PAPER_REVERSAL_MIN_DIP_PCT,
        "min_vol_ratio": settings.PAPER_REVERSAL_MIN_VOL_RATIO,
    }
    signal_date = await trade_calendar.previous_trade_day(trade_date)
    # 1. 上一交易日已收盘的反包涨停池记录。
    rows = (
        await db.execute(
            select(LimitUpPool)
            .where(
                LimitUpPool.trade_date == signal_date,
                LimitUpPool.quarantined.is_(False),
            )
            .order_by(desc(LimitUpPool.seal_amount))
        )
    ).scalars().all()
    if not rows:
        return [], [f"上一交易日{signal_date}涨停池无断板反包候选"]

    candidates: list[dict] = []
    rejected = 0
    for item in rows:
        code = str(item.code or "").strip()
        if not code or (only_code is not None and code != only_code):
            continue
        def reject(reason_code: str, reason: str, metric=None, threshold=None, *, data=False):
            if diagnostics is not None:
                diagnostics.append({
                    "code": code, "name": str(item.name or code),
                    "reason_code": reason_code, "reason": reason,
                    "stage_code": "data_gate" if data else "strategy_filter",
                    "metric_value": metric, "threshold_value": threshold,
                    "candidate": {
                        "signal_date": signal_date.isoformat(),
                        "entry_account": PAPER_ACCOUNT_REVERSAL,
                        "diagnostic_version": "reversal_first_reject_v1",
                        "diagnostic_scope": "first_rejection_per_stock",
                        "history_quality": "not_pit_certified_by_pattern_check",
                        "rule_snapshot": dict(cfg),
                        "requires_new_evidence": data,
                    },
                })
        # 2. 历史K线断板反包形态确认
        klines = (
            await db.execute(
                select(StockKline)
                .where(StockKline.code == code, StockKline.trade_date <= signal_date)
                .order_by(StockKline.trade_date.desc())
                .limit(30)
            )
        ).scalars().all()
        klines = list(reversed(klines))
        pattern_diagnostics: list[dict] = []
        pattern = _reversal_kline_pattern(
            klines, signal_date, cfg,
            **({"diagnostics": pattern_diagnostics} if diagnostics is not None else {}),
        )
        if not pattern:
            for failure in pattern_diagnostics:
                reject(failure["reason_code"], failure["reason"],
                       failure["metric_value"], failure["threshold_value"],
                       data=failure["stage_code"] == "data_gate")
            rejected += 1
            continue
        # 3. 实时 spot 确认: 已开板可交易 + 放量
        spot = await _spot_by_code(db, code)
        if not spot or not spot.price or float(spot.price) <= 0:
            reject("quote_missing", "本轮反包行情缺失或无效，等待新鲜报价", data=True)
            rejected += 1
            continue
        price = float(spot.price)
        change_pct = _to_float(getattr(spot, "change_pct", None))
        if (
            change_pct is None
            or change_pct > settings.PAPER_REVERSAL_MAX_INTRADAY_CONFIRM_CHANGE_PCT
        ):
            reject("change_missing" if change_pct is None else "entry_change_above_max",
                   "本轮涨幅缺失" if change_pct is None else "当前涨幅超过反包入口上限",
                   change_pct, settings.PAPER_REVERSAL_MAX_INTRADAY_CONFIRM_CHANGE_PCT,
                   data=change_pct is None)
            rejected += 1
            continue
        avg_price = _to_float(getattr(spot, "avg_price", None))
        if (
            settings.PAPER_REVERSAL_REQUIRE_ABOVE_VWAP
            and (avg_price is None or avg_price <= 0 or price < avg_price)
        ):
            missing_vwap = avg_price is None or avg_price <= 0
            reject("vwap_missing" if missing_vwap else "below_vwap",
                   "本轮VWAP缺失或无效" if missing_vwap else "现价低于VWAP",
                   price, avg_price, data=missing_vwap)
            rejected += 1
            continue
        high_price = _to_float(getattr(spot, "high", None))
        pullback_from_high_pct = (
            max(0.0, (high_price - price) / high_price * 100.0)
            if high_price is not None and high_price > 0
            else None
        )
        if (
            pullback_from_high_pct is None
            or pullback_from_high_pct
            > settings.PAPER_REVERSAL_MAX_PULLBACK_FROM_HIGH_PCT
        ):
            reject("high_missing" if pullback_from_high_pct is None else "high_pullback_above_max",
                   "本轮日内高点缺失" if pullback_from_high_pct is None else "高点回撤超过反包入口上限",
                   pullback_from_high_pct, settings.PAPER_REVERSAL_MAX_PULLBACK_FROM_HIGH_PCT,
                   data=pullback_from_high_pct is None)
            rejected += 1
            continue
        limit_up_price = float(getattr(spot, "limit_up", 0) or 0)
        if limit_up_price <= 0:
            reject("limit_price_missing", "本轮涨停价缺失，无法校验价格边界", data=True)
            rejected += 1
            continue
        if price >= limit_up_price * 0.998:
            # 仍封死/一字, 无法买入 → 等开板
            reject("reversal_current_at_limit", "本轮现价接近涨停，原策略等待开板",
                   price, limit_up_price * 0.998)
            rejected += 1
            continue
        # 放量使用已收盘信号日全日成交量，禁止把执行日盘中部分量与历史全日量比较。
        vol_ratio = float(pattern["vol_ratio"])
        # 4. ST/停牌/退市过滤 — tag 标记 或 stock_blacklist 命中即拦截 (2026-08-31 修复)
        tag = (
            await db.execute(select(StockTag).where(StockTag.code == code))
        ).scalar_one_or_none()
        blocked_reason = (
            await db.execute(
                select(StockBlacklist).where(
                    StockBlacklist.code == code,
                    StockBlacklist.reason.in_(["st", "delisting", "suspended"]),
                    _active_blacklist_clause(trade_date),
                )
            )
        ).scalar_one_or_none()
        if blocked_reason:
            reject("reversal_identity_blocked", f"当前有效黑名单：{blocked_reason.reason}")
            rejected += 1
            notes.append(f"{code} 在黑名单({blocked_reason.reason})中, 拒绝候选")
            continue
        if (
            not stock_tagger.is_tradeable(code)
            or (tag is not None and tag.board_tag != "tradeable")
            or (tag and (tag.is_st or tag.is_suspended or tag.is_delisting))
        ):
            reject("reversal_identity_blocked", "现有身份规则判为非可交易/ST/停牌/退市")
            rejected += 1
            continue

        vwap_premium_pct = (
            max(0.0, (price / avg_price - 1.0) * 100.0)
            if avg_price is not None and avg_price > 0
            else 0.0
        )
        execution_quality_score = min(
            99.0,
            55.0
            + pattern["consec_before"] * 6.0
            + min(6.0, vwap_premium_pct * 2.0)
            - min(6.0, (pullback_from_high_pct or 0.0) * 3.0),
        )
        stop_loss_pct = settings.PAPER_REVERSAL_STOP_LOSS_PCT
        stop_loss = price * (1 - stop_loss_pct / 100)
        candidates.append({
            "code": code,
            "name": str(item.name or spot.name or code),
            "_source": "reversal_pullback",
            "signal_source": "断板反包",
            "total_score": round(execution_quality_score, 2),
            "consecutive_days": pattern["consec_before"],
            "gap_days": pattern["gap_days"],
            "dip_min": pattern["dip_min"],
            "vol_ratio": round(vol_ratio, 2),
            "price": price,
            "change_pct": change_pct,
            "avg_price": avg_price,
            "high_price": high_price,
            "vwap_premium_pct": round(vwap_premium_pct, 2),
            "pullback_from_high_pct": round(pullback_from_high_pct or 0.0, 2),
            "stop_loss_price": round(stop_loss, 2),
            "stop_loss_pct": stop_loss_pct,
            "take_profit_pct": settings.PAPER_REVERSAL_TAKE_PROFIT_PCT,
            "max_hold_days": settings.PAPER_REVERSAL_MAX_HOLD_DAYS,
            "signal_date": signal_date.isoformat(),
            "strategy_label": "断板反包（基准策略）",
            "entry_condition": (
                f"{signal_date}反包确认：断板前连板{pattern['consec_before']}板, 间隔{pattern['gap_days']}日"
                f"(窗口最深{pattern['window_dip_pct']:.1f}%，前一日{pattern['prev_day_chg']:.1f}%), "
                f"全日量比{vol_ratio:.1f}；次日已开板可交易"
            ),
            "limit_up_reason": str(item.limit_up_reason or ""),
        })

    candidates.sort(
        key=lambda item: (
            _to_float(item.get("total_score")) or 0.0,
            int(item.get("consecutive_days") or 0),
            -int(item.get("gap_days") or 0),
            str(item.get("code") or ""),
        ),
        reverse=True,
    )
    if rejected:
        notes.append(
            f"已过滤{rejected}只(形态/涨幅上限/VWAP/高点回撤/盘口/ST)不达标的断板反包候选"
        )
    if not candidates:
        notes.append("断板反包候选均未通过执行日强度确认，继续仅作研究观察")
    return candidates[:limit], notes


def _midline_sell_reason(
    position: PaperPosition,
    ctx: dict,
    profit_pct: float,
    hold_days: int,
    params: Optional[dict] = None,
) -> str:
    """E/F只执行回放验证过的止损、止盈与交易日到期退出。"""
    price = _to_float(ctx.get("price"))
    stop_loss_price = _to_float(ctx.get("stop_loss_price")) or _to_float(position.stop_loss_price)
    take_profit_pct = _to_float((params or {}).get("take_profit_pct")) or settings.PAPER_HIGHBOARD_TAKE_PROFIT_PCT
    stop_loss_pct = _to_float((params or {}).get("stop_loss_pct")) or settings.PAPER_HIGHBOARD_STOP_LOSS_PCT
    max_hold_days = int((params or {}).get("max_hold_days") or settings.PAPER_HIGHBOARD_MAX_HOLD_DAYS)

    if price is not None and stop_loss_price and price <= stop_loss_price:
        return f"触发持仓止损价：现价{price:.2f} <= 止损{stop_loss_price:.2f}"
    if profit_pct <= -stop_loss_pct:
        return f"触发硬止损：{profit_pct:.2f}%"
    if profit_pct >= take_profit_pct:
        return f"触发短线止盈：{profit_pct:.2f}%"
    # 2026-09-17 改4：到期平仓不再无条件砍掉盈利仓。
    # 实测个案：账户12 603980 持仓 5 日到期时盈利 +1.80% 被平，
    # 而该账户的目标止盈是 +8.0% —— 属于"赢家被提前平掉"的同一类问题。
    # 现改为：到期时仅在未盈利时平仓；盈利仓给出宽限天数，
    # 满宽限后仍强制平仓以避免无限持有。
    # grace=0 时与修复前完全等价（到期无条件平仓）。
    expiry_grace_days = int((params or {}).get("expiry_grace_days")
                            if (params or {}).get("expiry_grace_days") is not None
                            else settings.PAPER_MIDLINE_EXPIRY_GRACE_DAYS)
    if hold_days >= max_hold_days:
        if profit_pct <= 0:
            return f"持仓{hold_days}个交易日到期平仓"
        if expiry_grace_days <= 0 or hold_days >= max_hold_days + expiry_grace_days:
            # grace<=0 时返回与修复前逐字相同的文案，保证可精确回退
            if expiry_grace_days <= 0:
                return f"持仓{hold_days}个交易日到期平仓"
            return (f"持仓{hold_days}个交易日到期平仓"
                    f"（盈利{profit_pct:.2f}%，已用满 {expiry_grace_days} 日宽限）")
    return ""


async def _daily_participation_candidates(db: AsyncSession, limit: int, *, trade_date: date | None = None) -> list[dict]:
    trade_date = trade_date or _paper_now().date()
    if not settings.PAPER_AUTO_DAILY_PARTICIPATION_ENABLED:
        return []

    # 每日参与不能使用“高分直接买”的旧兜底。这里先扩大到完整牛股观察榜，再用当前
    # 盘口快照确认量能、VWAP位置和低点回收；调度器每次运行都会重新判断，形成滚动观察。
    from app.api.v1.tenbagger import _bull_rank

    payload = await _bull_rank(db)
    rows = [
        dict(item)
        for item in (payload.get("rank") or [])
        if item.get("is_tradeable", True)
    ]
    codes = [str(item.get("code") or "").strip() for item in rows]
    codes = [code for code in codes if code]
    if not codes:
        return []
    spots = await _spots_by_codes(db, codes)
    spot_by_code = {spot.code: spot for spot in spots}

    selected: list[dict] = []
    allowed_levels = {
        value.strip().upper()
        for value in settings.PAPER_AUTO_DAILY_PARTICIPATION_ALLOWED_LEVELS.split(",")
        if value.strip()
    }
    for item in rows:
        code = str(item.get("code") or "").strip()
        spot = spot_by_code.get(code)
        base_score = _to_float(item.get("total_score")) or _to_float(item.get("score")) or 0
        short_trend_score = _to_float(item.get("short_trend_score")) or 0
        level = str(item.get("level") or "").strip().upper()
        if not spot or base_score < settings.PAPER_AUTO_DAILY_PARTICIPATION_MIN_SCORE:
            continue
        if level not in allowed_levels:
            continue
        if (
            short_trend_score < settings.PAPER_AUTO_DAILY_PARTICIPATION_MIN_SHORT_TREND_SCORE
            or not item.get("is_volume_price_uptrend")
        ):
            continue

        price = _to_float(spot.price)
        prev_close = _to_float(spot.prev_close)
        low = _to_float(spot.low)
        high = _to_float(spot.high)
        avg_price = _to_float(spot.avg_price)
        change_pct = _to_float(spot.change_pct)
        volume_ratio = _to_float(spot.volume_ratio)
        if not all(value is not None and value > 0 for value in (price, prev_close, low, high, avg_price)):
            continue
        if change_pct is None or not (
            settings.PAPER_AUTO_DAILY_PARTICIPATION_MIN_CHANGE_PCT
            <= change_pct
            <= settings.PAPER_AUTO_DAILY_PARTICIPATION_MAX_CHANGE_PCT
        ):
            continue
        if volume_ratio is None or not (
            settings.PAPER_AUTO_DAILY_PARTICIPATION_MIN_VOLUME_RATIO
            <= volume_ratio
            <= settings.PAPER_AUTO_DAILY_PARTICIPATION_MAX_VOLUME_RATIO
        ):
            continue

        rebound_from_low_pct = (price / low - 1) * 100
        close_position = (price - low) / max(high - low, 0.01)
        avg_premium_pct = (price / avg_price - 1) * 100
        if rebound_from_low_pct < settings.PAPER_AUTO_DAILY_PARTICIPATION_MIN_REBOUND_PCT:
            continue
        if rebound_from_low_pct > settings.PAPER_AUTO_DAILY_PARTICIPATION_MAX_REBOUND_PCT:
            continue
        if not (
            settings.PAPER_AUTO_DAILY_PARTICIPATION_MIN_CLOSE_POSITION
            <= close_position
            <= settings.PAPER_AUTO_DAILY_PARTICIPATION_MAX_CLOSE_POSITION
        ):
            continue
        if not (
            settings.PAPER_AUTO_DAILY_PARTICIPATION_MIN_AVG_PREMIUM_PCT
            <= avg_premium_pct
            <= settings.PAPER_AUTO_DAILY_PARTICIPATION_MAX_AVG_PREMIUM_PCT
        ):
            continue

        withdrawal_ratio = _to_float(getattr(spot, "withdrawal_ratio", None))
        orderbook_imbalance = _to_float(getattr(spot, "orderbook_imbalance", None))
        support_strength = _to_float(getattr(spot, "support_strength_score", None))
        fund = (await _paper_main_fund_map(db, trade_date=trade_date, codes=[code])).get(code)
        main_net_inflow = _to_float((fund or {}).get("main_net_inflow"))
        main_net_inflow_pct = _to_float((fund or {}).get("main_net_inflow_pct"))
        bid_ratio = _to_float(getattr(spot, "bid_ratio", None))
        if withdrawal_ratio is not None and withdrawal_ratio > 0.35:
            continue
        if orderbook_imbalance is not None and orderbook_imbalance < -0.25:
            continue

        liquidity_confirmations = sum((
            support_strength is not None
            and support_strength >= settings.PAPER_AUTO_DAILY_PARTICIPATION_MIN_SUPPORT_STRENGTH,
            orderbook_imbalance is not None
            and orderbook_imbalance >= settings.PAPER_AUTO_DAILY_PARTICIPATION_MIN_ORDERBOOK,
            main_net_inflow_pct is not None
            and main_net_inflow_pct >= settings.PAPER_AUTO_DAILY_PARTICIPATION_MIN_MAIN_INFLOW_PCT,
        ))
        if (
            liquidity_confirmations
            < settings.PAPER_AUTO_DAILY_PARTICIPATION_MIN_LIQUIDITY_CONFIRMATIONS
        ):
            continue

        confirmations: list[str] = []
        if short_trend_score >= 90:
            confirmations.append("短趋势保持强势")
        if item.get("is_volume_price_uptrend"):
            confirmations.append("日线量价仍向上")
        if -0.15 <= avg_premium_pct <= settings.PAPER_AUTO_DAILY_PARTICIPATION_MAX_AVG_PREMIUM_PCT:
            confirmations.append("回收并稳定在VWAP附近")
        if rebound_from_low_pct >= max(settings.PAPER_AUTO_DAILY_PARTICIPATION_MIN_REBOUND_PCT, 1.0):
            confirmations.append("低点回收明确")
        if (
            support_strength is not None
            and support_strength >= settings.PAPER_AUTO_DAILY_PARTICIPATION_MIN_SUPPORT_STRENGTH
        ):
            confirmations.append("五档承接增强")
        if (
            main_net_inflow_pct is not None
            and main_net_inflow_pct >= settings.PAPER_AUTO_DAILY_PARTICIPATION_MIN_MAIN_INFLOW_PCT
        ):
            confirmations.append("主力净流入占比达标")
        if (main_net_inflow is not None and main_net_inflow > 0) or (bid_ratio is not None and bid_ratio > 0):
            confirmations.append("增量买盘为正")
        if len(confirmations) < settings.PAPER_AUTO_DAILY_PARTICIPATION_MIN_CONFIRMATIONS:
            continue

        observation_score = min(
            96.0,
            base_score
            + min(max(short_trend_score - 80.0, 0.0) * 0.15, 3.0)
            + min(len(confirmations), 5) * 0.5,
        )
        if observation_score < settings.PAPER_AUTO_DRAWDOWN_CONTINUOUS_PARTICIPATION_MIN_SCORE:
            continue

        support_price = max(low, avg_price * 0.995)
        stop_loss = min(price * 0.985, low * 0.995)
        copied = dict(item)
        copied.update({
            "_source": "daily_participation",
            **_main_fund_evidence(fund),
            "signal_source": _PAPER_BUY_SOURCE_LABELS["daily_participation"],
            "daily_participation": True,
            "execution_confirmation": True,
            "total_score": round(observation_score, 2),
            "base_score": round(base_score, 2),
            "price": price,
            "change_pct": change_pct,
            "low_price": low,
            "high_price": high,
            "avg_price": avg_price,
            "volume_ratio": volume_ratio,
            "rebound_from_low_pct": round(rebound_from_low_pct, 2),
            "close_position": round(close_position, 3),
            "avg_premium_pct": round(avg_premium_pct, 2),
            "support": round(support_price, 2),
            "stop_loss_price": round(stop_loss, 2),
            "orderbook_imbalance": orderbook_imbalance,
            "support_strength_score": support_strength,
            "withdrawal_ratio": withdrawal_ratio,
            "main_net_inflow_pct": main_net_inflow_pct,
            "liquidity_confirmation_count": liquidity_confirmations,
            "intraday_confirmations": confirmations,
            "sector_reason": "滚动盘面确认：趋势未破、量能温和、低点回收且未脱离VWAP",
        })
        selected.append(copied)
    selected.sort(
        key=lambda item: (
            _to_float(item.get("total_score")) or 0,
            -abs(_to_float(item.get("avg_premium_pct")) or 0),
            _to_float(item.get("rebound_from_low_pct")) or 0,
        ),
        reverse=True,
    )
    return selected[:limit]


def _ma(values: list[float], window: int) -> Optional[float]:
    if len(values) < window:
        return None
    return round(sum(values[-window:]) / window, 4)


async def _ma5_pullback_candidates(
    db: AsyncSession,
    *,
    limit: int,
    trade_date: date,
) -> tuple[list[dict], list[str]]:
    if not settings.PAPER_AUTO_MA5_PULLBACK_ENABLED:
        return [], []

    rows = await _radar_candidates(db, limit=max(limit * 5, 30))
    selected: list[dict] = []
    rejected = 0
    for item in rows:
        code = str(item.get("code") or "").strip()
        score = _to_float(item.get("total_score")) or _to_float(item.get("score")) or 0
        if not code or score < settings.PAPER_AUTO_MA5_PULLBACK_MIN_SCORE:
            rejected += 1
            continue

        spot = await _spot_by_code(db, code)
        price = _to_float(getattr(spot, "price", None)) or _to_float(item.get("price"))
        change_pct = _to_float(getattr(spot, "change_pct", None))
        volume_ratio = _to_float(getattr(spot, "volume_ratio", None))
        low = _to_float(getattr(spot, "low", None))
        high = _to_float(getattr(spot, "high", None))
        avg_price = _to_float(getattr(spot, "avg_price", None))
        min5_change = _paper_effective_min5_change(spot, trade_date=trade_date)
        orderbook_imbalance = _to_float(getattr(spot, "orderbook_imbalance", None))
        support_strength = _to_float(getattr(spot, "support_strength_score", None))
        fund = (await _paper_main_fund_map(db, trade_date=trade_date, codes=[code])).get(code)
        main_net_inflow = _to_float((fund or {}).get("main_net_inflow"))
        if price is None or price <= 0 or change_pct is None or low is None:
            rejected += 1
            continue
        if (
            change_pct < settings.PAPER_AUTO_MA5_PULLBACK_MIN_CHANGE_PCT
            or change_pct > settings.PAPER_AUTO_MA5_PULLBACK_MAX_CHANGE_PCT
        ):
            rejected += 1
            continue
        if volume_ratio is not None and volume_ratio < settings.PAPER_AUTO_MA5_PULLBACK_MIN_VOLUME_RATIO:
            rejected += 1
            continue

        k_rows = await _latest_kline_rows(db, code, limit=21)
        closes = [_to_float(row.close) for row in k_rows if _to_float(row.close) is not None]
        if len(closes) < 20:
            rejected += 1
            continue
        ma5 = _ma(closes, 5)
        ma10 = _ma(closes, 10)
        ma20 = _ma(closes, 20)
        if ma5 is None or ma10 is None or ma20 is None:
            rejected += 1
            continue
        if not (ma5 > ma10 > ma20):
            rejected += 1
            continue

        ma5_distance_pct = (price / ma5 - 1) * 100
        five_day_change_pct = (closes[-1] / closes[-5] - 1) * 100 if closes[-5] > 0 else 999
        touched_ma5 = low <= ma5 * (1 + settings.PAPER_AUTO_MA5_PULLBACK_MAX_DIST_PCT / 100)
        held_ma5 = price >= ma5
        if (
            ma5_distance_pct < 0
            or ma5_distance_pct > settings.PAPER_AUTO_MA5_PULLBACK_MAX_DIST_PCT
            or not touched_ma5
            or not held_ma5
            or five_day_change_pct > settings.PAPER_AUTO_MA5_PULLBACK_MAX_5D_CHANGE_PCT
        ):
            rejected += 1
            continue

        close_position = (
            (price - low) / (high - low)
            if high is not None and high > low
            else None
        )
        rebound_from_low_pct = (price / low - 1) * 100
        avg_premium_pct = (
            (price / avg_price - 1) * 100
            if avg_price is not None and avg_price > 0
            else None
        )
        live_flow_confirmed = any((
            support_strength is not None and support_strength >= 55,
            orderbook_imbalance is not None and orderbook_imbalance >= 0.08,
            main_net_inflow is not None and main_net_inflow > 0,
        ))
        execution_confirmation = bool(
            avg_premium_pct is not None
            and -0.10 <= avg_premium_pct <= 0.50
            and close_position is not None
            and 0.30 <= close_position <= 0.75
            and min5_change is not None
            and -0.30 <= min5_change <= 0.80
            and volume_ratio is not None
            and volume_ratio <= settings.PAPER_AUTO_MA5_PULLBACK_MAX_VOLUME_RATIO
            and change_pct <= 1.8
            and live_flow_confirmed
        )
        stop_loss = min(ma10, ma5 * 0.985)
        copied = dict(item)
        copied.update({
            "_source": "ma5_pullback",
            **_main_fund_evidence(fund),
            "signal_source": _PAPER_BUY_SOURCE_LABELS["ma5_pullback"],
            "total_score": max(score, 90.0),
            "price": price,
            "change_pct": change_pct,
            "volume_ratio": volume_ratio,
            "low_price": low,
            "high_price": high,
            "avg_price": avg_price,
            "avg_premium_pct": round(avg_premium_pct, 2) if avg_premium_pct is not None else None,
            "rebound_from_low_pct": round(rebound_from_low_pct, 2),
            "close_position": round(close_position, 3) if close_position is not None else None,
            "min5_change": min5_change,
            "orderbook_imbalance": orderbook_imbalance,
            "support_strength_score": support_strength,
            "execution_confirmation": execution_confirmation,
            "ma5": ma5,
            "ma10": ma10,
            "ma20": ma20,
            "ma5_distance_pct": round(ma5_distance_pct, 2),
            "recent_5d_change_pct": round(five_day_change_pct, 2),
            "stop_loss_price": round(stop_loss, 2),
            "sector_reason": "上升通道缩量/温和回踩5日线，优先低吸不追高",
            "ma5_pullback": True,
        })
        selected.append(copied)
        if len(selected) >= limit:
            break

    notes = []
    if rejected:
        notes.append(f"5日线回踩扫描过滤{rejected}只趋势/位置/涨幅不达标标的")
    return selected, notes


async def _icepoint_reversal_candidates(db: AsyncSession, limit: int, *, trade_date: date | None = None) -> list[dict]:
    trade_date = trade_date or _paper_now().date()
    rows = await _radar_candidates(db, limit=max(limit * 4, 12))
    if db is None:
        return []
    codes = [str(item.get("code") or "").strip() for item in rows]
    codes = [code for code in codes if code]
    if not codes:
        return []
    spots = await _spots_by_codes(db, codes)
    spot_by_code = {spot.code: spot for spot in spots}
    selected: list[dict] = []
    for item in rows:
        score = _to_float(item.get("total_score")) or _to_float(item.get("score")) or 0
        code = str(item.get("code") or "").strip()
        spot = spot_by_code.get(code)
        if not spot:
            continue
        price = _to_float(spot.price)
        prev_close = _to_float(spot.prev_close)
        low = _to_float(spot.low)
        high = _to_float(spot.high)
        avg_price = _to_float(spot.avg_price)
        change_pct = _to_float(spot.change_pct)
        volume_ratio = _to_float(spot.volume_ratio)
        if not all(value is not None and value > 0 for value in (price, prev_close, low, high, avg_price)):
            continue
        if score < settings.PAPER_AUTO_ICEPOINT_REVERSAL_MIN_SCORE:
            continue
        if change_pct is None or change_pct < settings.PAPER_AUTO_ICEPOINT_REVERSAL_MIN_CHANGE_PCT:
            continue
        if change_pct > settings.PAPER_AUTO_ICEPOINT_REVERSAL_MAX_CHANGE_PCT:
            continue
        low_drop_pct = (low / prev_close - 1) * 100
        rebound_from_low_pct = (price / low - 1) * 100
        close_position = (price - low) / max(high - low, 0.01)
        avg_premium_pct = (price / avg_price - 1) * 100
        if low_drop_pct > -settings.PAPER_AUTO_ICEPOINT_REVERSAL_MIN_LOW_DROP_PCT:
            continue
        if rebound_from_low_pct < settings.PAPER_AUTO_ICEPOINT_REVERSAL_MIN_REBOUND_PCT:
            continue
        if not (
            settings.PAPER_AUTO_ICEPOINT_REVERSAL_MIN_CLOSE_POSITION
            <= close_position
            <= settings.PAPER_AUTO_ICEPOINT_REVERSAL_MAX_CLOSE_POSITION
        ):
            continue
        if avg_premium_pct < -0.2 or avg_premium_pct > settings.PAPER_AUTO_ICEPOINT_REVERSAL_MAX_AVG_PREMIUM_PCT:
            continue
        if volume_ratio is None or not (
            settings.PAPER_AUTO_ICEPOINT_REVERSAL_MIN_VOLUME_RATIO
            <= volume_ratio
            <= settings.PAPER_AUTO_ICEPOINT_REVERSAL_MAX_VOLUME_RATIO
        ):
            continue
        bid_ratio = _to_float(getattr(spot, "bid_ratio", None))
        fund = (await _paper_main_fund_map(db, trade_date=trade_date, codes=[code])).get(code)
        main_net_inflow = _to_float((fund or {}).get("main_net_inflow"))
        if (bid_ratio is None or bid_ratio <= 0) and (main_net_inflow is None or main_net_inflow <= 0):
            continue
        copied = dict(item)
        copied.update({
            "_source": "icepoint_reversal",
            **_main_fund_evidence(fund),
            "icepoint_reversal": True,
            "execution_confirmation": True,
            "price": price,
            "change_pct": change_pct,
            "low_price": low,
            "high_price": high,
            "avg_price": avg_price,
            "volume_ratio": volume_ratio,
            "low_drop_pct": round(low_drop_pct, 2),
            "rebound_from_low_pct": round(rebound_from_low_pct, 2),
            "close_position": round(close_position, 3),
            "avg_premium_pct": round(avg_premium_pct, 2),
            "sector_reason": "冰点环境先下探后放量回收，价格仍靠近VWAP，按低吸而非追涨处理",
        })
        selected.append(copied)
        if len(selected) >= limit:
            break
    return selected


async def _radar_candidates(db: AsyncSession, limit: int) -> list[dict]:
    from app.api.v1.tenbagger import _bull_rank

    payload = await _bull_rank(db)
    rows = payload.get("rank", [])
    candidates = [
        row for row in rows
        if row.get("is_volume_price_uptrend")
        and row.get("is_tradeable", True)
        and float(row.get("total_score") or 0) >= settings.PAPER_AUTO_MIN_SCORE
    ]
    candidates.sort(
        key=lambda item: (
            float(item.get("short_trend_score") or 0),
            float(item.get("total_score") or 0),
            float(item.get("change_pct") or 0),
        ),
        reverse=True,
    )
    result = []
    for item in candidates[:limit]:
        copied = dict(item)
        copied["_source"] = "radar"
        result.append(copied)
    return result


async def _strategy_candidates(db: AsyncSession, limit: int) -> tuple[list[dict], Optional[str]]:
    from app.api.v1.backtest import preview_backtest_candidates

    payload = await preview_backtest_candidates(
        min_score=70,
        max_positions=settings.PAPER_AUTO_MAX_POSITIONS,
        limit=limit,
        db=db,
    )
    signal_date = payload.get("signal_date")
    rows = payload.get("candidates") or []
    selected = []
    for item in rows:
        if item.get("decision") != "入选":
            continue
        copied = dict(item)
        copied["_source"] = "strategy"
        selected.append(copied)
    return selected[:limit], signal_date


async def _latest_kline_rows(db: AsyncSession, code: str, limit: int = 6) -> list[StockKline]:
    result = await db.execute(
        select(StockKline)
        .where(StockKline.code == code)
        .order_by(desc(StockKline.trade_date))
        .limit(limit)
    )
    return list(reversed(result.scalars().all()))


async def _recent_kline_change_pct(db: AsyncSession, code: str, limit: int = 3) -> Optional[float]:
    rows = await _latest_kline_rows(db, code, limit=limit)
    if len(rows) < 2:
        return None
    first_close = _to_float(rows[0].close)
    last_close = _to_float(rows[-1].close)
    if not first_close or first_close <= 0 or last_close is None:
        return None
    return round((last_close / first_close - 1) * 100, 2)


async def _recent_kline_rows_before(db: AsyncSession, code: str, before: date, limit: int = 2) -> list[StockKline]:
    result = await db.execute(
        select(StockKline)
        .where(StockKline.code == code, StockKline.trade_date < before)
        .order_by(desc(StockKline.trade_date))
        .limit(limit)
    )
    return list(reversed(result.scalars().all()))


async def _weak_rebound_reject_reason(
    db: AsyncSession,
    *,
    code: str,
    trade_date: date,
    price: float,
    change_pct: Optional[float] = None,
    event_types: Optional[list[str]] = None,
) -> str:
    prev_rows = await _recent_kline_rows_before(db, code, trade_date, limit=2)
    if len(prev_rows) < 2:
        return ""
    prev_changes = [_to_float(row.change_pct) for row in prev_rows]
    two_day_drop = all(value is not None and value <= -settings.PAPER_AUTO_REBOUND_DROP_PCT for value in prev_changes)
    crash_repair = any(value is not None and value <= -settings.PAPER_AUTO_REBOUND_CRASH_PCT for value in prev_changes)
    cluster_rows = await _recent_kline_rows_before(
        db,
        code,
        trade_date,
        limit=settings.PAPER_AUTO_REBOUND_CLUSTER_DAYS,
    )
    cluster_changes = [_to_float(row.change_pct) for row in cluster_rows]
    cluster_drop_count = sum(
        1
        for value in cluster_changes
        if value is not None and value <= -settings.PAPER_AUTO_REBOUND_CRASH_PCT
    )
    cluster_damage = cluster_drop_count >= settings.PAPER_AUTO_REBOUND_CLUSTER_DROP_COUNT
    if not two_day_drop and not crash_repair and not cluster_damage:
        return ""

    spot = await _spot_by_code(db, code)
    volume_ratio = _to_float(getattr(spot, "volume_ratio", None))
    avg_price = _to_float(getattr(spot, "avg_price", None))
    orderbook_imbalance = _to_float(getattr(spot, "orderbook_imbalance", None))
    event_set = {str(item) for item in (event_types or [])}
    weak_parts = []
    if two_day_drop and (volume_ratio is None or volume_ratio < settings.PAPER_AUTO_REBOUND_MIN_VOLUME_RATIO):
        weak_parts.append(f"量比{volume_ratio if volume_ratio is not None else '--'}<1")
    if two_day_drop and avg_price is not None and price < avg_price:
        weak_parts.append(f"现价{price:.2f}<均价{avg_price:.2f}")
    if two_day_drop and orderbook_imbalance is not None and orderbook_imbalance < -0.35:
        weak_parts.append(f"盘口卖压{orderbook_imbalance:.2f}")
    if (
        crash_repair
        and (change_pct is None or change_pct < settings.PAPER_AUTO_REBOUND_CONFIRM_CHANGE_PCT)
        and not event_set.intersection({"breakthrough", "rapid_rise"})
    ):
        weak_parts.append(
            f"单日大跌修复涨幅{change_pct if change_pct is not None else '--'}"
            f"<{settings.PAPER_AUTO_REBOUND_CONFIRM_CHANGE_PCT:.1f}%且无突破/快速拉升"
        )
    if (
        cluster_damage
        and (change_pct is None or change_pct < settings.PAPER_AUTO_REBOUND_CLUSTER_CONFIRM_CHANGE_PCT)
        and "rapid_rise" not in event_set
    ):
        weak_parts.append(
            f"{settings.PAPER_AUTO_REBOUND_CLUSTER_DAYS}日内{cluster_drop_count}次大跌，"
            f"修复涨幅{change_pct if change_pct is not None else '--'}"
            f"<{settings.PAPER_AUTO_REBOUND_CLUSTER_CONFIRM_CHANGE_PCT:.1f}%"
        )
    if weak_parts:
        changes = "/".join(f"{value:.2f}%" for value in cluster_changes[-2:] if value is not None)
        return f"连续大跌后弱反抽未确认：近两日{changes}，" + "，".join(weak_parts)
    return ""


async def _continuation_risk_reject_reason(
    db: AsyncSession,
    *,
    code: str,
    trade_date: date,
    candidate: Optional[dict] = None,
) -> str:
    candidate = candidate or {}
    spot = await _spot_by_code(db, code)
    prev_rows = await _recent_kline_rows_before(db, code, trade_date, limit=1)
    if prev_rows:
        prev = prev_rows[-1]
        prev_close = _to_float(prev.close)
        prev_change = _to_float(prev.change_pct)
        prev_limit = (
            await db.execute(
                select(LimitUpPool)
                .where(LimitUpPool.code == code, LimitUpPool.trade_date == prev.trade_date)
                .limit(1)
            )
        ).scalar_one_or_none()
        prev_was_limit_up = bool(prev_limit) or (prev_change is not None and prev_change >= 9.5)
        open_price = _to_float(getattr(spot, "open", None))
        current_change = _to_float(getattr(spot, "change_pct", None))
        if current_change is None:
            current_change = _to_float(candidate.get("change_pct"))
        if prev_was_limit_up and prev_close and prev_close > 0 and open_price and open_price > 0:
            open_gap_pct = (open_price / prev_close - 1) * 100
            if open_gap_pct <= -settings.PAPER_AUTO_CONTINUATION_LIMITUP_GAP_RISK_PCT:
                return (
                    f"昨日涨停后今日大幅低开{open_gap_pct:.2f}%，"
                    "隔日延续性不足，短线不开新仓"
                )
        if (
            prev_was_limit_up
            and current_change is not None
            and current_change <= settings.PAPER_AUTO_CONTINUATION_LIMITUP_WEAK_CHANGE_PCT
        ):
            return (
                f"昨日涨停后今日涨幅{current_change:.2f}%未延续，"
                "有涨停次日转弱风险"
            )

    source = str(candidate.get("_source") or "")
    change_pct = _to_float(candidate.get("change_pct"))
    sector_days = int(candidate.get("sector_consecutive_days") or 0)
    sector_strength = _to_float(candidate.get("sector_strength")) or 0.0
    sector_change_pct = _to_float(candidate.get("sector_change_pct")) or 0.0
    sector_fund_flow = _to_float(candidate.get("sector_fund_flow")) or 0.0
    limit_up_count = int(candidate.get("sector_limit_up_count") or 0)
    leader_first_move = bool(candidate.get("leader_first_move"))
    theme_spread = bool(candidate.get("theme_spread"))
    if _is_reversal_buy_source(source):
        if (
            change_pct is not None
            and change_pct >= settings.PAPER_AUTO_GREEN_REVERSAL_NEAR_LIMIT_CHANGE_PCT
            and (
                sector_strength < settings.PAPER_AUTO_GREEN_REVERSAL_NEAR_LIMIT_MIN_SECTOR_STRENGTH
                or sector_fund_flow <= 0
            )
        ):
            return (
                f"绿盘转强已接近涨停{change_pct:.2f}%，但板块强度{sector_strength:.1f}"
                f"或资金{sector_fund_flow:.2f}亿不配合，不追弱板块高位"
            )
        if (
            leader_first_move
            and not theme_spread
            and (
                sector_strength < settings.PAPER_AUTO_GREEN_REVERSAL_LEADER_MIN_SECTOR_STRENGTH
                or sector_fund_flow <= 0
            )
        ):
            return (
                f"个股先手但板块强度{sector_strength:.1f}<"
                f"{settings.PAPER_AUTO_GREEN_REVERSAL_LEADER_MIN_SECTOR_STRENGTH:.1f}"
                f"或资金{sector_fund_flow:.2f}亿未配合，短线不做孤立冲高"
            )
        if (
            not leader_first_move
            and not theme_spread
            and sector_strength < settings.PAPER_AUTO_GREEN_REVERSAL_MIN_SECTOR_STRENGTH
        ):
            return (
                f"绿盘转强但板块强度{sector_strength:.1f}<"
                f"{settings.PAPER_AUTO_GREEN_REVERSAL_MIN_SECTOR_STRENGTH:.1f}，"
                "不是强主线打板"
            )
        if (
            not leader_first_move
            and not theme_spread
            and sector_change_pct < settings.PAPER_AUTO_GREEN_REVERSAL_MIN_SECTOR_CHANGE_PCT
        ):
            return (
                f"绿盘转强但板块涨幅{sector_change_pct:.2f}%<"
                f"{settings.PAPER_AUTO_GREEN_REVERSAL_MIN_SECTOR_CHANGE_PCT:.2f}%，"
                "板块没有同步进攻"
            )
        if not leader_first_move and not theme_spread and sector_fund_flow <= 0:
            return f"绿盘转强但板块资金{sector_fund_flow:.2f}亿净流出，防冲高回落"

        price = _to_float(candidate.get("price")) or _to_float(getattr(spot, "price", None)) or 0.0
        limit_up = _to_float(getattr(spot, "limit_up", None))
        seal_quality = _to_float(getattr(spot, "seal_quality_score", None))
        if (
            price > 0
            and limit_up
            and limit_up > 0
            and price >= limit_up * 0.999
            and (seal_quality is None or seal_quality < settings.PAPER_AUTO_GREEN_REVERSAL_MIN_SEAL_QUALITY)
        ):
            return (
                f"已冲到涨停价但封单质量{seal_quality if seal_quality is not None else '--'}"
                f"<{settings.PAPER_AUTO_GREEN_REVERSAL_MIN_SEAL_QUALITY:.0f}，不打未封硬板"
            )
    if (
        _is_reversal_buy_source(source)
        and not leader_first_move
        and not theme_spread
        and change_pct is not None
        and change_pct >= settings.PAPER_AUTO_GREEN_REVERSAL_MIN_CHANGE_PCT
        and (
            sector_days < settings.PAPER_AUTO_CONTINUATION_MIN_SECTOR_DAYS
            or sector_strength < settings.PAPER_AUTO_CONTINUATION_MIN_SECTOR_STRENGTH
            or limit_up_count < settings.PAPER_AUTO_CONTINUATION_MIN_LIMIT_UP_COUNT
        )
    ):
        return (
            f"近涨停但板块持续{sector_days}日、强度{sector_strength:.1f}、"
            f"涨停{limit_up_count}家，持续性不足，防一日游"
        )
    return ""


def _candidate_sector_key(candidate: dict) -> str:
    return str(
        candidate.get("sector_name")
        or candidate.get("sector_resonance_name")
        or candidate.get("driver_primary")
        or ""
    ).strip()


async def _sector_retreat_reason(
    db: AsyncSession,
    code: str,
    entry_sector_code: str | None = None,
    entry_sector_name: str | None = None,
    entry_trade_date: Optional[date] = None,
    trade_date: Optional[date] = None,
    observed_before: Optional[datetime] = None,
) -> str:
    """优先复核首次建仓板块；交易决策只采信指定交易日的板块快照。

    2026-09-17 复盘修复：本表按 (sector_code, trade_date) upsert，盘中的值会被
    盘后终值覆盖。此前读取侧只按 trade_date 过滤，回放/审计时可能采信"决策时刻
    之后才写入"的板块强度（前视偏差）。传入 observed_before 后，只采信在决策时刻
    之前已落库的行；盘中尚无该日快照时宁可返回空，也不使用未来值。
    """
    def _snapshot_query():
        stmt = select(SectorPersistence)
        if observed_before is not None:
            stmt = stmt.where(
                or_(
                    SectorPersistence.observed_at.is_(None),
                    SectorPersistence.observed_at <= observed_before,
                )
            )
        return stmt

    selected_code = str(entry_sector_code or "").strip()
    selected_name = str(entry_sector_name or "").strip()
    if selected_code and entry_trade_date is not None:
        entry_snapshot_exists = (await db.execute(
            _snapshot_query()
            .where(
                SectorPersistence.sector_code == selected_code,
                SectorPersistence.trade_date == entry_trade_date,
            )
            .limit(1)
        )).scalar_one_or_none()
        if entry_snapshot_exists is None:
            return ""
    latest = None
    if selected_code:
        persistence_stmt = _snapshot_query().where(
            SectorPersistence.sector_code == selected_code
        )
        if trade_date is not None:
            persistence_stmt = persistence_stmt.where(
                SectorPersistence.trade_date == trade_date
            )
        latest = (await db.execute(
            persistence_stmt
            .order_by(desc(SectorPersistence.trade_date), desc(SectorPersistence.id))
            .limit(1)
        )).scalar_one_or_none()
    else:
        contexts = await _ranked_sector_contexts_for_code(
            db,
            code,
            trade_date=trade_date,
        )
        if selected_name:
            context = next(
                (
                    item for item in contexts
                    if selected_name in {
                        str(item.get("sector_name") or ""),
                        str(item.get("_mapping_name") or ""),
                    }
                ),
                None,
            )
        else:
            context = contexts[0] if contexts else None
        if context:
            selected_code = str(context.get("sector_code") or "")
            selected_name = str(context.get("sector_name") or selected_name)
            latest = context.get("_persistence")
    if latest is None:
        return ""

    sector_label = latest.sector_name or selected_name or selected_code
    strength = _to_float(latest.strength_score)
    change_pct = _to_float(latest.change_pct)
    fund_flow = _to_float(latest.fund_flow)
    limit_up_count = int(latest.limit_up_count or 0)
    if strength is not None and strength < settings.PAPER_AUTO_SECTOR_RETREAT_STRENGTH:
        return f"板块退潮：{sector_label}强度{strength:.1f}"
    if change_pct is not None and change_pct <= -1.0 and limit_up_count <= 0:
        return f"板块退潮：{sector_label}跌幅{change_pct:.2f}%且无涨停支撑"
    if fund_flow is not None and fund_flow < 0 and limit_up_count <= 0:
        return f"板块退潮：{sector_label}资金净流出且无涨停支撑"
    return ""


async def _build_short_sell_context(db: AsyncSession, position: PaperPosition, trade_date: Optional[date] = None) -> dict:
    spot = await _spot_by_code(db, position.code)
    # 2026-08-31: 拉 21 根 K 线以支持 MA10/MA20 计算 (修复策略E"跌破MA20"死代码)
    k_rows = await _latest_kline_rows(db, position.code, limit=21)
    latest_k = k_rows[-1] if k_rows else None
    closes = [_to_float(row.close) for row in k_rows]
    closes = [value for value in closes if value is not None]
    ma5 = round(sum(closes[-5:]) / 5, 4) if len(closes) >= 5 else None
    ma10 = round(sum(closes[-10:]) / 10, 4) if len(closes) >= 10 else None
    ma20 = round(sum(closes[-20:]) / 20, 4) if len(closes) >= 20 else None

    price = _to_float(getattr(spot, "price", None)) or _to_float(getattr(latest_k, "close", None)) or position.current_price or position.buy_price
    open_price = _to_float(getattr(spot, "open", None)) or _to_float(getattr(latest_k, "open", None))
    high = _to_float(getattr(spot, "high", None)) or _to_float(getattr(latest_k, "high", None))
    low = _to_float(getattr(spot, "low", None)) or _to_float(getattr(latest_k, "low", None))
    change_pct = _to_float(getattr(spot, "change_pct", None)) or _to_float(getattr(latest_k, "change_pct", None))
    limit_down = _to_float(getattr(spot, "limit_down", None))
    volume_ratio = _to_float(getattr(spot, "volume_ratio", None))
    if volume_ratio is None and len(k_rows) >= 6:
        today_volume = _to_float(k_rows[-1].volume)
        prev_volumes = [_to_float(row.volume) for row in k_rows[-6:-1]]
        prev_volumes = [value for value in prev_volumes if value and value > 0]
        if today_volume and prev_volumes:
            volume_ratio = round(today_volume / (sum(prev_volumes) / len(prev_volumes)), 2)

    close_position = None
    if price is not None and high is not None and low is not None and high > low:
        close_position = round((price - low) / (high - low), 2)

    prev_kline = None
    if trade_date:
        prev_rows = await _recent_kline_rows_before(db, position.code, trade_date, limit=1)
        prev_kline = prev_rows[-1] if prev_rows else None
    elif len(k_rows) >= 2:
        prev_kline = k_rows[-2]
    prev_change_pct = _to_float(getattr(prev_kline, "change_pct", None))
    prev_close = _to_float(getattr(prev_kline, "close", None))
    prev_prev_close = _to_float(getattr(prev_kline, "prev_close", None))
    prev_was_limit_up = bool(
        prev_change_pct is not None
        and prev_change_pct >= 9.5
    ) or bool(
        prev_close
        and prev_prev_close
        and prev_prev_close > 0
        and prev_close >= prev_prev_close * 1.095
    )
    open_gap_from_prev_close_pct = None
    if open_price is not None and prev_close and prev_close > 0:
        open_gap_from_prev_close_pct = round((open_price / prev_close - 1) * 100, 2)

    return {
        "price": price,
        "open": open_price,
        "high": high,
        "low": low,
        "change_pct": change_pct,
        "limit_down": limit_down,
        "volume_ratio": volume_ratio,
        "ma5": ma5,
        "ma10": ma10,
        "ma20": ma20,
        "avg_price": _to_float(getattr(spot, "avg_price", None)),
        "min5_change": _paper_effective_min5_change(
            spot,
            trade_date=trade_date or date.today(),
        ),
        "orderbook_imbalance": _to_float(getattr(spot, "orderbook_imbalance", None)),
        "bid_ask_spread": _to_float(getattr(spot, "bid_ask_spread", None)),
        "close_position": close_position,
        "sector_retreat_reason": await _sector_retreat_reason(
            db,
            position.code,
            getattr(position, "entry_sector_code", None),
            getattr(position, "entry_sector_name", None),
            entry_trade_date=(position.buy_time.date() if position.buy_time else None),
            trade_date=trade_date,
            # 只采信在该报价可见之前已落库的板块快照：sector_persistence 按
            # (sector_code, trade_date) upsert，盘中值会被盘后终值覆盖，
            # 若不限定观测基准，回放/审计会采信决策时刻之后才写入的强度（前视偏差）。
            observed_before=getattr(spot, "received_at", None) or _paper_now(),
        ),
        "stop_loss_price": _to_float(position.stop_loss_price),
        "prev_was_limit_up": prev_was_limit_up,
        "prev_change_pct": prev_change_pct,
        "prev_close": prev_close,
        "open_gap_from_prev_close_pct": open_gap_from_prev_close_pct,
    }


def _short_sell_reason(
    position: PaperPosition,
    ctx: dict,
    profit_pct: float,
    hold_days: int,
    trade_date: date,
    now: Optional[datetime] = None,
    params: Optional[dict] = None,
) -> str:
    """短线卖出原因判定.

    params: 可选参数覆盖 (双策略并行 2026-08-31).
      策略A(default): None → 用 settings.PAPER_AUTO_* 默认参数
      策略B(promotion): 传入 {take_profit_pct, stop_loss_pct, max_hold_days, ...}
    """
    take_profit_pct = _number_or((params or {}).get("take_profit_pct"), settings.PAPER_AUTO_TAKE_PROFIT_PCT)
    stop_loss_pct = _number_or((params or {}).get("stop_loss_pct"), settings.PAPER_AUTO_STOP_LOSS_PCT)
    small_stop_loss_pct = _number_or((params or {}).get("small_stop_loss_pct"), settings.PAPER_AUTO_SMALL_STOP_LOSS_PCT)
    open_severe_stop_loss_pct = _number_or((params or {}).get("open_severe_stop_loss_pct"), settings.PAPER_AUTO_OPEN_SEVERE_STOP_LOSS_PCT)
    max_hold_days = int((params or {}).get("max_hold_days") or settings.PAPER_AUTO_MAX_HOLD_DAYS)
    next_day_min_profit_pct = _number_or((params or {}).get("next_day_min_profit_pct"), settings.PAPER_AUTO_NEXT_DAY_MIN_PROFIT_PCT)
    pullback_from_high_pct = _number_or((params or {}).get("pullback_from_high_pct"), settings.PAPER_AUTO_PULLBACK_FROM_HIGH_PCT)
    breakeven_protect_high_profit_pct = _number_or((params or {}).get("breakeven_protect_high_profit_pct"), settings.PAPER_AUTO_BREAKEVEN_PROTECT_HIGH_PROFIT_PCT)
    breakeven_protect_low_pct = _number_or((params or {}).get("breakeven_protect_low_pct"), settings.PAPER_AUTO_BREAKEVEN_PROTECT_LOW_PCT)
    breakeven_protect_high_pct = _number_or((params or {}).get("breakeven_protect_high_pct"), settings.PAPER_AUTO_BREAKEVEN_PROTECT_HIGH_PCT)

    price = _to_float(ctx.get("price"))
    open_price = _to_float(ctx.get("open"))
    high = _to_float(ctx.get("high"))
    change_pct = _to_float(ctx.get("change_pct"))
    limit_down = _to_float(ctx.get("limit_down"))
    volume_ratio = _to_float(ctx.get("volume_ratio"))
    ma5 = _to_float(ctx.get("ma5"))
    avg_price = _to_float(ctx.get("avg_price"))
    min5_change = _to_float(ctx.get("min5_change"))
    orderbook_imbalance = _to_float(ctx.get("orderbook_imbalance"))
    close_position = _to_float(ctx.get("close_position"))
    sector_retreat_reason = str(ctx.get("sector_retreat_reason") or "")
    stop_loss_price = _to_float(ctx.get("stop_loss_price")) or _to_float(position.stop_loss_price)
    prev_was_limit_up = bool(ctx.get("prev_was_limit_up"))
    open_gap_from_prev_close_pct = _to_float(ctx.get("open_gap_from_prev_close_pct"))
    open_noise = _is_open_noise_window(now, end_value=(params or {}).get("open_noise_end"))
    weak_confirmations = _short_weak_confirmation_count(
        price=price,
        open_price=open_price,
        avg_price=avg_price,
        ma5=ma5,
        orderbook_imbalance=orderbook_imbalance,
        volume_ratio=volume_ratio,
        change_pct=change_pct,
    )
    # 2026-09-17 复盘修复：窗内门槛改用独立证据，避免同义反复造成的假豁免。
    independent_evidence = _short_independent_weak_evidence_count(
        price=price,
        open_price=open_price,
        avg_price=avg_price,
        ma5=ma5,
        min5_change=min5_change,
        orderbook_imbalance=orderbook_imbalance,
        volume_ratio=volume_ratio,
        change_pct=change_pct,
    )
    open_noise_stop_min_evidence = int(
        (params or {}).get("open_noise_stop_min_evidence")
        if (params or {}).get("open_noise_stop_min_evidence") is not None
        else settings.PAPER_AUTO_OPEN_NOISE_STOP_MIN_EVIDENCE
    )
    open_noise_weak_min_evidence = int(
        (params or {}).get("open_noise_weak_min_evidence")
        if (params or {}).get("open_noise_weak_min_evidence") is not None
        else settings.PAPER_AUTO_OPEN_NOISE_WEAK_MIN_EVIDENCE
    )
    # 2026-09-17 复盘 改1/改3：窗外弱信号 rung 的独立证据门槛。
    # 这些 rung 只依赖"价格低于某条参考线"，此前在开盘噪声窗之外零门槛，
    # 实测 62% 的盈利持仓被它们提前平掉（平均只拿到 +1.51%，止盈组 +9.90%）。
    weak_exit_min_evidence = int(
        (params or {}).get("weak_exit_min_evidence")
        if (params or {}).get("weak_exit_min_evidence") is not None
        else settings.PAPER_AUTO_WEAK_EXIT_MIN_EVIDENCE
    )
    # 冷门门槛：0 = 关闭（恢复修复前行为），否则要求 independent_evidence 达标
    weak_exit_allowed = (
        weak_exit_min_evidence <= 0
        or independent_evidence >= weak_exit_min_evidence
    )

    if prev_was_limit_up:
        if price is not None and limit_down and limit_down > 0 and price <= limit_down * 1.002:
            return f"昨日涨停次日跌停风险：现价{price:.2f}接近跌停{limit_down:.2f}，可卖仓全退"
        if open_gap_from_prev_close_pct is not None and open_gap_from_prev_close_pct <= -3.0:
            return f"昨日涨停次日转弱：低开{open_gap_from_prev_close_pct:.2f}%，延续性失败全退"
        if change_pct is not None and change_pct <= -5.0:
            return f"昨日涨停次日转弱：跌幅{change_pct:.2f}%，延续性失败全退"

    if price is not None and stop_loss_price and price <= stop_loss_price:
        if (
            open_noise
            and open_noise_stop_min_evidence > 0
            and profit_pct > -open_severe_stop_loss_pct
            and independent_evidence < open_noise_stop_min_evidence
        ):
            return ""
        return f"触发持仓止损价：现价{price:.2f} <= 止损{stop_loss_price:.2f}"
    if profit_pct <= -stop_loss_pct:
        if (
            open_noise
            and open_noise_stop_min_evidence > 0
            and profit_pct > -open_severe_stop_loss_pct
            and independent_evidence < open_noise_stop_min_evidence
        ):
            return ""
        return f"触发硬止损：{profit_pct:.2f}%"
    if open_noise and independent_evidence < open_noise_weak_min_evidence:
        return ""
    if profit_pct <= -small_stop_loss_pct:
        if weak_confirmations >= 2 or profit_pct <= -(small_stop_loss_pct + 1.0):
            return f"跌破买入价小止损：{profit_pct:.2f}%"
    if profit_pct >= take_profit_pct:
        return f"触发短线止盈：{profit_pct:.2f}%"
    if high is not None and price is not None and position.buy_price:
        high_profit = (high / position.buy_price - 1) * 100
        pullback = (price / high - 1) * 100 if high else 0
        if (
            weak_exit_allowed
            and high_profit >= breakeven_protect_high_profit_pct
            and breakeven_protect_low_pct <= profit_pct <= breakeven_protect_high_pct
        ):
            return f"回落成本线保护：当日日高相对成本{high_profit:.2f}%（非持仓最高浮盈），当前盈亏{profit_pct:.2f}%"
        if weak_exit_allowed and high_profit >= 1.8 and pullback <= -pullback_from_high_pct:
            return f"盘中冲高回落：当日日高相对成本{high_profit:.2f}%（非持仓最高浮盈），从当日日高回落{abs(pullback):.2f}%"
        if weak_exit_allowed and high_profit >= 1.8 and close_position is not None and close_position < 0.35:
            return f"盘中收弱：当日日高相对成本{high_profit:.2f}%（非持仓最高浮盈），收盘位置{close_position:.2f}"
    if (weak_exit_allowed and price is not None and avg_price is not None
            and price < avg_price and profit_pct <= 0.5 and hold_days >= 1):
        return f"跌破分时均价：现价{price:.2f} < 均价{avg_price:.2f}，短线转弱"
    if (weak_exit_allowed and price is not None and open_price is not None
            and price < open_price and profit_pct <= 0.5 and hold_days >= 1):
        return f"跌破开盘价：现价{price:.2f} < 开盘{open_price:.2f}，短线转弱"
    if min5_change is not None and min5_change <= -1.0 and profit_pct <= 1.0 and hold_days >= 1:
        return f"5分钟急跌：{min5_change:.2f}%"
    if orderbook_imbalance is not None and orderbook_imbalance <= -0.35 and profit_pct <= 1.0 and hold_days >= 1:
        return f"盘口卖压增强：五档失衡{orderbook_imbalance:.2f}"
    if weak_exit_allowed and hold_days >= 1 and profit_pct < next_day_min_profit_pct:
        weak_parts = []
        if change_pct is not None:
            weak_parts.append(f"当日涨幅{change_pct:.2f}%")
        if ma5 is not None and price is not None:
            weak_parts.append(f"现价{price:.2f}/MA5 {ma5:.2f}")
        detail = "，".join(weak_parts) if weak_parts else f"盈亏{profit_pct:.2f}%"
        return f"次日不强就走：{detail}"
    if (weak_exit_allowed and ma5 is not None and price is not None
            and price < ma5 and hold_days >= 1):
        return f"跌破5日线：现价{price:.2f} < MA5 {ma5:.2f}"
    if (
        volume_ratio is not None
        and volume_ratio >= _number_or((params or {}).get("volume_negative_ratio"), settings.PAPER_AUTO_VOLUME_NEGATIVE_RATIO)
        and change_pct is not None
        and change_pct < 0
        and price is not None
        and open_price is not None
        and price < open_price
    ):
        return f"放量阴线：量比{volume_ratio:.2f}，跌幅{change_pct:.2f}%"
    confirmed_sector_retreat = _confirmed_sector_retreat_sell_reason(
        sector_retreat_reason,
        price=price,
        open_price=open_price,
        avg_price=avg_price,
        ma5=ma5,
        min5_change=min5_change,
        orderbook_imbalance=orderbook_imbalance,
        volume_ratio=volume_ratio,
        change_pct=change_pct,
        close_position=close_position,
    )
    if confirmed_sector_retreat:
        return confirmed_sector_retreat
    if hold_days >= max_hold_days and profit_pct <= 0:
        return f"持仓{hold_days}天未转强，时间止损"
    return ""


def _strategy_sell_params(account: PaperAccount) -> dict:
    """按账户策略返回卖出参数覆盖 (五策略并行 2026-08-31).

    策略A/B/C/D返回各自短线参数；策略E/F返回与中线硬退出逻辑一致的
    参数供执行与评估共享，避免接口误用策略A阈值。
    """
    account_name = str(getattr(account, "account_name", "") or "")
    return _strategy_sell_params_by_name(account_name)


async def _run_auto_sells(
    db: AsyncSession,
    *,
    account: PaperAccount,
    run_id: str,
    trade_date: date,
    trigger: str,
    execute: bool,
    order_strategy_id: str = "paper-auto-short",
    order_signal_prefix: str = "auto-sell",
    order_source: str = "position",
    forced_exit_reason_by_code: Optional[dict[str, str]] = None,
    quote_now: Optional[datetime] = None,
    log_holds: bool = True,
) -> list[PaperAutoTradeLog]:
    from app.paper.position_policy import position_exit_policy
    from app.paper.exit_audit import observe_position_extrema, attach_exit_audit, block_exit
    try:
        from app.paper.position_observation import (
            capture_position_frame, append_position_frames, capture_sell_execution_frame,
            append_deferred_execution_frames as append_sell_execution_frames,
        )
    except Exception as observation_exc:
        # Optional research availability must not disable existing risk exits.
        capture_position_frame = append_position_frames = None
        capture_sell_execution_frame = append_sell_execution_frames = None
        logger.warning("[paper] position observation import unavailable (%s)", type(observation_exc).__name__)

    logs = []
    position_frames = []
    submission_frames = []
    positions = await _open_positions(db, account.id)
    default_sell_params = _strategy_sell_params(account)
    base_account_name = _base_strategy_account(str(account.account_name or ""))
    is_midline = (base_account_name in (PAPER_ACCOUNT_TENBAGGER, PAPER_ACCOUNT_REVERSAL)
                  or account.account_name == PAPER_ACCOUNT_CHALLENGER_A)
    for position in positions:
        await _refresh_expired_paper_rows(db, account, position)
        if getattr(position, "is_closed", False):
            continue
        sell_params, exit_policy = await position_exit_policy(
            db, account_name=str(account.account_name), position=position,
            defaults=default_sell_params, as_of=quote_now or _paper_now(),
        )
        sell_ctx = await _build_short_sell_context(db, position, trade_date)
        sell_ctx["exit_policy"] = exit_policy
        sell_ctx["exit_parameters"] = sell_params
        spot = await _spot_by_code(db, position.code)
        quote_ok, quote_reason = _execution_quote_status(
            spot,
            trade_date,
            now=quote_now,
        )
        if quote_ok:
            sell_ctx["price"] = _to_float(getattr(spot, "price", None))
            sell_ctx["quote_updated_at"] = getattr(spot, "updated_at", None)
        try:
            # 附加审计失败不能阻塞既有合法风险退出，保存点只回滚本次审计写入。
            async with db.begin_nested():
                extrema = await observe_position_extrema(
                    db, position=position, spot=spot, observed_at=quote_now or _paper_now(),
                    quote_ok=quote_ok, max_age_sec=settings.PAPER_EXECUTION_QUOTE_MAX_AGE_SEC,
                    persist=execute,
                )
        except OperationalError:
            # A rolled-back SAVEPOINT can leave a stale SQLite read snapshot.
            # The account boundary must discard this transaction before retrying.
            raise
        except Exception as audit_exc:
            extrema = {"coverage": "audit_unavailable", "error_type": type(audit_exc).__name__}
        attach_exit_audit(sell_ctx, position=position, extrema=extrema, quote_ok=quote_ok)
        price = float(sell_ctx.get("price") or position.current_price or position.buy_price or 0)
        profit_pct = float(position.profit_pct or 0)
        if position.buy_price:
            profit_pct = round((price / position.buy_price - 1) * 100, 2)
        hold_days = int(position.hold_days or 0)
        forced_exit_reason = str(
            (forced_exit_reason_by_code or {}).get(position.code) or ""
        ).strip()
        if forced_exit_reason:
            # 失效版本/证据污染的模拟仓位保留历史，不回写删除；在A股T+1
            # 首个可卖窗口按最新盘口退出，避免继续拿旧规则承担风险。
            reason = forced_exit_reason
        elif is_midline:
            # E/F及对应次账户、A2均消费自身建仓时退出规则，不再临时继承主账户设置。
            reason = _midline_sell_reason(position, sell_ctx, profit_pct, hold_days, params=sell_params)
        else:
            reason = _short_sell_reason(
                position,
                sell_ctx,
                profit_pct,
                hold_days,
                trade_date,
                quote_now or datetime.now(),
                params=sell_params,
            )
        # 策略触发与执行拦截并列保存；后续T+1/盘口/订单分支不得覆盖原触发。
        sell_ctx["exit_trigger_reason"] = reason
        sell_ctx["exit_execution_status"] = "evaluating" if reason else "not_requested"
        if execute and capture_position_frame is not None:
            # Freeze only what this real pass observed; do not run extra risk/T+1
            # checks for research. Append after all original sell branches finish.
            try:
                frame = capture_position_frame(position=position,
                    account_name=str(account.account_name), trade_date=trade_date, quote=spot,
                    quote_context=_quote_round_context(), evaluated_at=quote_now or _paper_now(),
                    quote_ok=quote_ok, quote_reason=quote_reason, exit_parameters=sell_params,
                    exit_policy=exit_policy, trigger_reason=reason)
                if frame is not None:
                    position_frames.append((frame, sell_ctx))
            except Exception as observation_exc:
                logger.warning("[paper] position observation unavailable (%s)", type(observation_exc).__name__)
        if not reason:
            if not log_holds:
                continue
            logs.append(await _add_auto_log(
                db,
                account_id=account.id,
                run_id=run_id,
                trade_date=trade_date,
                trigger=trigger,
                source="position",
                code=position.code,
                name=position.name or "",
                action="hold",
                decision="wait",
                reason=f"继续持有：持仓{hold_days}个交易日，盈亏{profit_pct:.2f}%，卖点未触发",
                price=price,
                amount=position.buy_amount,
                candidate=sell_ctx,
            ))
            continue

        available_amount = await _available_sell_amount(db, position, trade_date)
        sell_ctx["available_sell_amount"] = available_amount
        if available_amount < 100:
            reason = (
                f"A股T+1：当天买入不能当天卖出；已标记下个可卖窗口退出（{forced_exit_reason}）"
                if forced_exit_reason
                else "A股T+1：当天买入不能当天卖出"
            )
            block_exit(sell_ctx, code="t_plus_one", reason=reason)
        elif not quote_ok:
            block_exit(sell_ctx, code="quote_invalid", reason=f"卖出行情硬门槛：{quote_reason}")
            logs.append(await _add_auto_log(
                db,
                account_id=account.id,
                run_id=run_id,
                trade_date=trade_date,
                trigger=trigger,
                source="position",
                code=position.code,
                name=position.name or "",
                action="skip_sell",
                decision="blocked",
                reason=f"卖出行情硬门槛：{quote_reason}",
                price=price or None,
                amount=available_amount,
                candidate=sell_ctx,
            ))
            continue
        else:
            execution_price = _conservative_execution_price(spot, "sell")
            if execution_price is None:
                block_exit(sell_ctx, code="orderbook_unfillable", reason="卖出盘口不可成交或跌停封死，禁止模拟成交")
                logs.append(await _add_auto_log(
                    db,
                    account_id=account.id,
                    run_id=run_id,
                    trade_date=trade_date,
                    trigger=trigger,
                    source="position",
                    code=position.code,
                    name=position.name or "",
                    action="skip_sell",
                    decision="blocked",
                    reason="卖出盘口不可成交或跌停封死，禁止模拟成交",
                    price=price or None,
                    amount=available_amount,
                    candidate=sell_ctx,
                ))
                continue
            sell_ctx["market_price"] = price
            sell_ctx["execution_price"] = execution_price
            price = execution_price
            today_sell_stats = await _today_sell_stats(db, account.id, position.code, trade_date)
            if (
                not is_midline
                and int(today_sell_stats.get("amount") or 0) >= 100
                and not _is_full_exit_reason(reason)
            ):
                if _is_post_t_protect_exit_reason(reason):
                    sell_amount = available_amount
                    reason = f"T后保护卖出：{reason}"
                else:
                    block_exit(sell_ctx, code="intraday_t_already_sold", reason="今日已做T减仓，等待回补或保护卖点", status="waiting")
                    logs.append(await _add_auto_log(
                        db,
                        account_id=account.id,
                        run_id=run_id,
                        trade_date=trade_date,
                        trigger=trigger,
                        source="position",
                        code=position.code,
                        name=position.name or "",
                        action="hold",
                        decision="wait",
                        reason="今日已做T减仓，等待回补或保护卖点",
                        price=price,
                        amount=position.buy_amount,
                        candidate=sell_ctx,
                    ))
                    continue
            else:
                sell_amount = (
                    available_amount
                    if is_midline or forced_exit_reason
                    else _auto_sell_amount(position, available_amount, reason, params=sell_params)
                )

        if reason.startswith("A股T+1"):
            # 2026-09-17 复盘修复：同一 T+1 阻塞状态按 (账户,股票,交易日,原因) 去重，
            # 首条落库、其后仅刷新末次复现时间，避免单日数千条重复日志。
            t1_log = await _log_t1_skip_once(
                db, run_id=run_id, trade_date=trade_date, trigger=trigger,
                source="position", code=position.code, name=position.name or "",
                reason=reason, price=price, amount=position.buy_amount,
                candidate=sell_ctx, account_id=account.id,
            )
            if t1_log is not None:
                logs.append(t1_log)
            continue
        if sell_amount < 100:
            block_exit(sell_ctx, code="sell_lot_insufficient", reason="可卖隔夜仓不足一手，自动T/卖出跳过")
            logs.append(await _add_auto_log(
                db,
                account_id=account.id,
                run_id=run_id,
                trade_date=trade_date,
                trigger=trigger,
                source="position",
                code=position.code,
                name=position.name or "",
                action="skip_sell",
                decision="skipped",
                reason="可卖隔夜仓不足一手，自动T/卖出跳过",
                price=price,
                amount=available_amount,
                candidate=sell_ctx,
            ))
            continue

        if not is_midline and sell_amount < int(position.buy_amount or 0):
            reason = f"T减仓{sell_amount}股：{reason}"

        sell_ctx["exit_order_reason"] = reason
        if not execute:
            sell_ctx["exit_execution_status"] = "dry_run"
            logs.append(await _add_auto_log(
                db,
                account_id=account.id,
                run_id=run_id,
                trade_date=trade_date,
                trigger=trigger,
                source="position",
                code=position.code,
                name=position.name or "",
                action="sell",
                decision="dry_run",
                reason=reason,
                price=price,
                amount=sell_amount,
                candidate=sell_ctx,
            ))
            continue

        if await _has_active_paper_order(
            db,
            account_name=account.account_name,
            code=position.code,
            side="sell",
            trade_date=trade_date,
        ):
            block_exit(sell_ctx, code="active_sell_order", reason="已有未终结卖出委托，等待下一健康报价轮次继续按深度撮合", status="order_pending")
            logs.append(await _add_auto_log(
                db,
                account_id=account.id,
                run_id=run_id,
                trade_date=trade_date,
                trigger=trigger,
                source="position",
                code=position.code,
                name=position.name or "",
                action="deferred_sell",
                decision="wait",
                reason="已有未终结卖出委托，等待下一健康报价轮次继续按深度撮合",
                price=price,
                amount=sell_amount,
                candidate=sell_ctx,
            ))
            continue

        try:
            from app.trading.service import SubmitOrderCommand, submit_order

            sell_ctx["exit_execution_status"] = "submitting"
            quote_context = _quote_round_context()
            decision_round_id = str(quote_context.get("round_id") or "")
            result = await submit_order(
                db,
                SubmitOrderCommand(
                    code=position.code,
                    side="sell",
                    price=price,
                    quantity=sell_amount,
                    broker="paper",
                    account_id=account.account_name,   # 五策略并行: 成交落账到当前策略账户 (2026-08-31)
                    strategy_id=order_strategy_id,
                    strategy_version=str(position.strategy_version or "legacy_unversioned"),
                    signal_id=(
                        f"{order_signal_prefix}-{decision_round_id or trade_date.strftime('%Y%m%d')}-{position.code}"
                    )[:80],
                    source=order_source,
                    reason=reason,
                    execute=execute,
                    decision_round_id=decision_round_id,
                    decision_at=quote_now or _paper_now(),
                    as_of_at=(
                        quote_context.get("as_of_at")
                        if isinstance(quote_context.get("as_of_at"), datetime)
                        else None
                    ),
                    idempotency_key=(
                        f"{decision_round_id}:{account.account_name}:{order_source}:sell:{position.code}"
                        if decision_round_id
                        else ""
                    ),
                    defer_until_next_round=bool(
                        settings.PAPER_DEFER_AUTO_FILL_TO_NEXT_ROUND
                    ),
                    deferred_metadata={
                        "candidate": sell_ctx,
                        "position_id": position.id,
                        # 入场版本用于历史归因；退出决策单独冻结当前执行版本。
                        "exit_decision_strategy_version": _strategy_version(account.account_name),
                        # warn 级别不能阻塞减仓/止损，真正 block 仍会拦截。
                        "block_warn": False,
                    },
                ),
            )
            await _refresh_expired_paper_rows(db, account, position, *logs)
            if capture_sell_execution_frame is not None:
                try:
                    response_frame = capture_sell_execution_frame(
                        account_id=account.id, account_name=str(account.account_name),
                        trade_date=trade_date, quote_context=quote_context,
                        evaluated_at=quote_now or _paper_now(), outcome=result,
                        origin="order_submission", position=position,
                        trigger_reason=sell_ctx.get("exit_trigger_reason"))
                    if response_frame is not None:
                        submission_frames.append(response_frame)
                except Exception as observation_exc:
                    logger.warning("[paper] sell response observation unavailable (%s)", type(observation_exc).__name__)
            order = result.get("order") or {}
            fills = result.get("fills") or []
            trade_id = fills[0].get("broker_trade_id") if fills else None
            order_status = str(order.get("status") or "")
            sell_filled = order_status == "filled" and bool(fills)
            sell_pending = order_status in {"submitted", "partial"}
            sell_ctx["exit_order_status"] = order_status
            sell_ctx["exit_order_id"] = order.get("order_id")
            sell_ctx["exit_execution_status"] = "filled" if sell_filled else "order_pending" if sell_pending else "blocked"
            if not sell_filled and not sell_pending:
                block_exit(sell_ctx, code="order_not_accepted", reason=str(order.get("error_message") or order_status or "未知委托状态"))
            logs.append(await _add_auto_log(
                db,
                account_id=account.id,
                run_id=run_id,
                trade_date=trade_date,
                trigger=trigger,
                source="position",
                code=position.code,
                name=position.name or "",
                action=(
                    "sell"
                    if sell_filled
                    else "deferred_sell"
                    if sell_pending
                    else "skip_sell"
                ),
                decision=(
                    "executed"
                    if sell_filled
                    else "wait"
                    if sell_pending
                    else "blocked"
                ),
                reason=(
                    reason
                    if sell_filled
                    else str(order.get("error_message") or reason)
                    if sell_pending
                    else f"{reason}；委托未成交：{order.get('error_message') or order_status or '未知状态'}"
                ),
                price=price,
                amount=sell_amount,
                candidate=sell_ctx,
                executed_trade_id=trade_id if sell_filled else None,
            ))
        except OperationalError:
            # Never try to flush a failure log in the failed business transaction.
            raise
        except Exception as exc:
            from app.trading.paper_authorization import paper_execution_requires_reconciliation
            if paper_execution_requires_reconciliation(exc):
                # A commit may already be durable. Do not fabricate skip_sell/blocked
                # evidence or continue entries after an unknown execution outcome.
                logger.error("[paper] 卖出执行结果待核对，停止本账户本轮执行 (%s)", type(exc).__name__)
                raise
            block_exit(sell_ctx, code="execution_error", reason=f"卖出执行异常：{type(exc).__name__}")
            logs.append(await _add_auto_log(
                db,
                account_id=account.id,
                run_id=run_id,
                trade_date=trade_date,
                trigger=trigger,
                source="position",
                code=position.code,
                name=position.name or "",
                action="skip_sell",
                decision="blocked",
                reason=f"卖出失败：{exc}",
                price=price,
                amount=sell_amount,
                candidate=sell_ctx,
            ))
    # Separate research route/event type never enters auto-log/push/return counts.
    # The helper uses a Core savepoint and cannot flush pending business objects.
    if append_position_frames is not None:
        try:
            await append_position_frames(db, position_frames)
        except Exception as observation_exc:
            logger.warning("[paper] position observation append unavailable (%s)", type(observation_exc).__name__)
    if append_sell_execution_frames is not None:
        try:
            # Same isolated execution-observation route, never a confirmed signal.
            await append_sell_execution_frames(db, submission_frames)
        except Exception as observation_exc:
            logger.warning("[paper] sell response append unavailable (%s)", type(observation_exc).__name__)
    return logs


def _t_buyback_reject_reason(position, stats: dict, ctx: dict, price: float) -> str:
    """Shared original T predicates; no new-entry route or threshold substitution."""
    sell_price = _to_float(stats.get("avg_price"))
    if not sell_price or sell_price <= 0:
        return "T回补缺少有效卖出均价"
    drop_pct = (price / sell_price - 1) * 100
    max_price = float(position.buy_price or 0) * (
        1 + settings.PAPER_AUTO_T_BUYBACK_MAX_ABOVE_COST_PCT / 100)
    if drop_pct > -settings.PAPER_AUTO_T_BUYBACK_DROP_PCT:
        return f"T回补等待：现价较卖出价回落{abs(drop_pct):.2f}%，未达{settings.PAPER_AUTO_T_BUYBACK_DROP_PCT:.1f}%"
    if max_price > 0 and price > max_price:
        return f"T回补跳过：现价{price:.2f}高于成本保护价{max_price:.2f}，不把已兑现利润重新接高"
    avg_price = _to_float(ctx.get("avg_price"))
    if avg_price is not None and price < avg_price:
        return f"T回补跳过：现价{price:.2f}<分时均价{avg_price:.2f}，弱势回落不接"
    if ctx.get("sector_retreat_reason"):
        return f"T回补跳过：{ctx['sector_retreat_reason']}"
    min5 = _to_float(ctx.get("min5_change"))
    imbalance = _to_float(ctx.get("orderbook_imbalance"))
    if min5 is not None and min5 <= -1.5:
        return "T回补跳过：5分钟动能失效"
    if imbalance is not None and imbalance <= -0.35:
        return "T回补跳过：盘口承接失效"
    return ""


def _t_buyback_identity(position, stats: dict, count: int) -> dict:
    return {
        "account_id": position.account_id, "position_id": position.id,
        "code": position.code, "strategy_version": position.strategy_version,
        "buy_time": position.buy_time.isoformat() if position.buy_time else None,
        "buy_price": float(position.buy_price or 0),
        "buy_amount": int(position.buy_amount or 0),
        "entry_sector_code": getattr(position, "entry_sector_code", None),
        "entry_sector_name": getattr(position, "entry_sector_name", None),
        "sell_amount": int(stats.get("amount") or 0),
        "sell_avg_price": _to_float(stats.get("avg_price")),
        "sell_first_time": stats["first_time"].isoformat() if stats.get("first_time") else None,
        "buyback_count": count,
    }


async def _freeze_t_buyback_identity(db, cmd, candidate: dict, decision_at: datetime):
    from app.trading.service import _pending_buy_clock

    if (cmd.source != "position-t" or candidate.get("code") != cmd.code
            or candidate.get("_source") != "position-t"):
        return None, "T原始候选代码/来源缺失或不一致"
    evidence = candidate.get("t_buyback")
    if not isinstance(evidence, dict):
        return None, "T原持仓合同缺失"
    account = await db.scalar(select(PaperAccount).where(
        PaperAccount.id == evidence.get("account_id"),
        PaperAccount.account_name == cmd.account_id, PaperAccount.status == "active"))
    if account is None:
        return None, "T原active账户身份失效"
    position = await db.scalar(select(PaperPosition).where(
        PaperPosition.id == evidence.get("position_id"),
        PaperPosition.account_id == account.id, PaperPosition.code == cmd.code,
        PaperPosition.is_closed.is_(False)))
    if position is None or position.strategy_version != cmd.strategy_version or cmd.strategy_version != _strategy_version(cmd.account_id):
        return None, "T原持仓或策略版本失效"
    stats = await _today_sell_stats(db, account.id, cmd.code, decision_at.date(), as_of=decision_at)
    count = await _today_t_buyback_count(db, account.id, cmd.code, decision_at.date(), as_of=decision_at)
    actual = _t_buyback_identity(position, stats, count)
    first = _pending_buy_clock(actual["sell_first_time"])
    if (actual != evidence or first is None or first > decision_at
            or first.date() != decision_at.date()
            or actual["sell_amount"] < cmd.quantity or actual["buy_price"] <= 0):
        return None, "T原卖出统计/持仓证据已改变或不可验证"
    return actual, ""


async def _pending_t_buyback_confirmation(db, *, order, contract: dict, spot, now: datetime):
    evidence = contract.get("t_buyback")
    if not isinstance(evidence, dict) or order.source != "position-t":
        return "canceled", "T原持仓合同缺失，禁止套用新开仓路线"
    account = await db.scalar(select(PaperAccount).where(
        PaperAccount.id == evidence.get("account_id"),
        PaperAccount.account_name == order.account_id, PaperAccount.status == "active"))
    if account is None:
        return "canceled", "T原账户身份失效"
    position = await db.scalar(select(PaperPosition).where(
        PaperPosition.id == evidence.get("position_id"), PaperPosition.account_id == account.id,
        PaperPosition.code == order.code, PaperPosition.is_closed.is_(False)))
    if (position is None or position.strategy_version != evidence.get("strategy_version")
            or position.strategy_version != order.strategy_version
            or order.strategy_version != _strategy_version(order.account_id)):
        return "canceled", "T原持仓或策略版本失效"
    stats = await _today_sell_stats(db, account.id, order.code, order.trade_date, as_of=now)
    count = await _today_t_buyback_count(db, account.id, order.code, order.trade_date, as_of=now)
    current = _t_buyback_identity(position, stats, count)
    # A partial fill may change cost/size; do not silently mint a fresh T contract.
    if current != evidence:
        return "canceled", "T原卖出统计/持仓状态已改变，撤销余量"
    if not settings.PAPER_AUTO_TRADE_T_ENABLED or count >= settings.PAPER_AUTO_T_BUYBACK_MAX_PER_DAY:
        return "canceled", "T回补已关闭或达到当日次数上限"
    if getattr(spot, "code", None) != order.code:
        return "canceled", "T本轮行情代码不一致"
    ctx = await _build_short_sell_context(db, position, trade_date=order.trade_date)
    # Missing live evidence waits, never reuses the frozen candidate to pass.
    ctx["avg_price"] = _to_float(getattr(spot, "avg_price", None))
    ctx["orderbook_imbalance"] = _to_float(getattr(spot, "orderbook_imbalance", None))
    if (ctx["avg_price"] is None or ctx["avg_price"] <= 0
            or ctx["orderbook_imbalance"] is None or _to_float(ctx.get("min5_change")) is None):
        return "waiting", "暂缺T回补VWAP/动能/盘口证据"
    price = _conservative_execution_price(spot, "buy")
    if price is None:
        return "waiting", "暂缺T回补可成交盘口"
    reason = _t_buyback_reject_reason(position, stats, ctx, price)
    return ("canceled", reason) if reason else ("valid", "")


async def _run_auto_t_buybacks(
    db: AsyncSession,
    *,
    account: PaperAccount,
    run_id: str,
    trade_date: date,
    trigger: str,
    execute: bool,
) -> list[PaperAutoTradeLog]:
    if not settings.PAPER_AUTO_TRADE_T_ENABLED:
        return []
    if str(account.account_name or "") in {PAPER_ACCOUNT_TENBAGGER, PAPER_ACCOUNT_REVERSAL}:
        return []

    logs = []
    positions = await _open_positions(db, account.id)
    for position in positions:
        await _refresh_expired_paper_rows(db, account, position)
        if getattr(position, "is_closed", False):
            continue
        t_observed_at = _paper_now()
        stats = await _today_sell_stats(db, account.id, position.code, trade_date, as_of=t_observed_at)
        sold_amount = _round_lot(int(stats.get("amount") or 0))
        sell_price = _to_float(stats.get("avg_price"))
        if sold_amount < 100 or not sell_price:
            continue
        current_strategy_version = _strategy_version(account.account_name)
        position_strategy_version = str(
            getattr(position, "strategy_version", "") or ""
        )
        if position_strategy_version != current_strategy_version:
            logs.append(await _add_auto_log(
                db,
                account_id=account.id,
                run_id=run_id,
                trade_date=trade_date,
                trigger=trigger,
                source="position-t",
                code=position.code,
                name=position.name or "",
                action="skip_buy",
                decision="blocked",
                reason=(
                    "禁止跨策略版本T回补："
                    f"持仓版本={position_strategy_version or 'legacy_unversioned'}，"
                    f"当前版本={current_strategy_version}"
                ),
                amount=sold_amount,
            ))
            continue

        ctx = await _build_short_sell_context(db, position)
        spot = await _spot_by_code(db, position.code)
        quote_ok, quote_reason = _execution_quote_status(spot, trade_date)
        if not quote_ok:
            logs.append(await _add_auto_log(
                db,
                account_id=account.id,
                run_id=run_id,
                trade_date=trade_date,
                trigger=trigger,
                source="position-t",
                code=position.code,
                name=position.name or "",
                action="skip_buy",
                decision="blocked",
                reason=f"T回补行情硬门槛：{quote_reason}",
                price=_to_float(getattr(spot, "price", None)) if spot else None,
                amount=sold_amount,
                candidate=ctx,
            ))
            continue
        market_price = float(spot.price)
        price = _conservative_execution_price(spot, "buy")
        if price is None:
            logs.append(await _add_auto_log(
                db,
                account_id=account.id,
                run_id=run_id,
                trade_date=trade_date,
                trigger=trigger,
                source="position-t",
                code=position.code,
                name=position.name or "",
                action="skip_buy",
                decision="blocked",
                reason="T回补盘口不可成交或涨停封死，禁止模拟成交",
                price=market_price,
                amount=sold_amount,
                candidate=ctx,
            ))
            continue
        ctx["market_price"] = market_price
        ctx["execution_price"] = price
        ctx["quote_updated_at"] = getattr(spot, "updated_at", None)
        buyback_count = await _today_t_buyback_count(
            db, account.id, position.code, trade_date, as_of=t_observed_at)
        if buyback_count >= settings.PAPER_AUTO_T_BUYBACK_MAX_PER_DAY:
            logs.append(await _add_auto_log(
                db,
                account_id=account.id,
                run_id=run_id,
                trade_date=trade_date,
                trigger=trigger,
                source="position-t",
                code=position.code,
                name=position.name or "",
                action="hold",
                decision="wait",
                reason=f"今日T回补已执行{buyback_count}次，达到上限{settings.PAPER_AUTO_T_BUYBACK_MAX_PER_DAY}次，不再反复接回",
                price=price,
                amount=sold_amount,
                candidate=ctx,
            ))
            continue
        drop_pct = (price / sell_price - 1) * 100
        min5_change = _to_float(ctx.get("min5_change"))
        orderbook_imbalance = _to_float(ctx.get("orderbook_imbalance"))
        avg_price = _to_float(ctx.get("avg_price"))
        max_buyback_price = float(position.buy_price or 0) * (
            1 + settings.PAPER_AUTO_T_BUYBACK_MAX_ABOVE_COST_PCT / 100
        )
        t_reason = _t_buyback_reject_reason(position, stats, ctx, price)
        if t_reason:
            logs.append(await _add_auto_log(
                db, account_id=account.id, run_id=run_id, trade_date=trade_date,
                trigger=trigger, source="position-t", code=position.code,
                name=position.name or "", action="hold", decision="wait",
                reason=t_reason, price=price, amount=sold_amount, candidate=ctx,
            ))
            continue
        confirmed_at = _paper_now()
        ctx.update({
            "code": position.code, "_source": "position-t",
            "t_buyback": _t_buyback_identity(position, stats, buyback_count),
        })

        # T回补也是买点：在自身价格/成本/承接过滤之后、预算风控之前留证。
        # 旧交易规则允许部分辅助字段缺失；这种情况不冒充完整买点推送。
        if execute and avg_price is not None and avg_price > 0 and min5_change is not None and orderbook_imbalance is not None:
            from app.push.paper_buy_points import record_buy_point
            signal_context = _quote_round_context()
            await record_buy_point(
                db, account=account, strategy_version=current_strategy_version,
                label=_strategy_display_meta(account)["label"] + " · 做T回补",
                code=position.code, name=position.name or "", source="position-t",
                signal_key=f"{position.id}:{stats.get('first_time')}:{buyback_count}",
                reason=(
                    f"做T回补：已卖出的隔夜仓回接；较当日卖出均价{sell_price:.2f}"
                    f"回落{abs(drop_pct):.2f}%，达到自身回补门槛；"
                    f"未超过成本保护价{max_buyback_price:.2f}；"
                    f"VWAP{avg_price:.2f}、盘口失衡{orderbook_imbalance:.2f}"
                    "通过回补过滤，板块未触发退潮拦截"
                ),
                price=market_price, observed_at=_paper_now(), decision_run_id=run_id,
                quote_round_id=str(signal_context.get("round_id") or ""),
                as_of_at=signal_context.get("as_of_at"),
                code_version=str(signal_context.get("code_version") or ""),
                market_context={
                    **{key: getattr(spot, key, None) for key in (
                        "price", "prev_close", "open", "high", "low", "change_pct",
                        "avg_price", "volume_ratio", "turnover_rate", "amount",
                        "circ_market_cap", "limit_up", "limit_down",
                        "orderbook_imbalance", "bid_ask_spread",
                    )},
                    "source_quote_at": str(getattr(spot, "source_quote_at", "") or ""),
                    "quote_round_id": str(getattr(spot, "quote_round_id", "") or ""),
                },
            )
        cash_amount = _round_lot((float(account.current_capital or 0) * 0.98) // price)
        amount = min(sold_amount, cash_amount)
        if amount < 100:
            logs.append(await _add_auto_log(
                db, account_id=account.id, run_id=run_id, trade_date=trade_date,
                trigger=trigger, source="position-t", code=position.code, name=position.name or "",
                action="skip_buy", decision="skipped",
                reason="T回补买点已到，但当前可用资金不足一手", price=price,
                amount=amount, candidate=ctx,
            ))
            continue

        reason = f"日内做T回补：较卖出均价{sell_price:.2f}回落{abs(drop_pct):.2f}%"
        risk = await _risk_check_for_buy(db, account, position.code, price, amount)
        if risk.get("final_level") == "block":
            block_reason = "；".join(item.get("message", "") for item in risk.get("block_reasons", [])) or "风控拦截"
            logs.append(await _add_auto_log(
                db,
                account_id=account.id,
                run_id=run_id,
                trade_date=trade_date,
                trigger=trigger,
                source="position-t",
                code=position.code,
                name=position.name or "",
                action="skip_buy",
                decision="blocked",
                reason=block_reason,
                price=price,
                amount=amount,
                risk_level=risk.get("final_level"),
                risk=risk,
                candidate=ctx,
            ))
            continue
        if execute and settings.PAPER_AUTO_WARN_RISK_BLOCK_BUY and risk.get("final_level") == "warn":
            warning_reason = "；".join(item.get("message", "") for item in risk.get("warnings", [])) or "风控警告"
            logs.append(await _add_auto_log(
                db,
                account_id=account.id,
                run_id=run_id,
                trade_date=trade_date,
                trigger=trigger,
                source="position-t",
                code=position.code,
                name=position.name or "",
                action="skip_buy",
                decision="blocked",
                reason=f"{warning_reason}，自动模拟模式不放行warn级别T回补",
                price=price,
                amount=amount,
                risk_level=risk.get("final_level"),
                risk=risk,
                candidate=ctx,
            ))
            continue

        if not execute:
            logs.append(await _add_auto_log(
                db,
                account_id=account.id,
                run_id=run_id,
                trade_date=trade_date,
                trigger=trigger,
                source="position-t",
                code=position.code,
                name=position.name or "",
                action="buy",
                decision="dry_run",
                reason=reason,
                price=price,
                amount=amount,
                risk_level=risk.get("final_level"),
                risk=risk,
                candidate=ctx,
            ))
            continue

        if await _has_active_paper_order(
            db,
            account_name=account.account_name,
            code=position.code,
            side="buy",
            trade_date=trade_date,
        ):
            logs.append(await _add_auto_log(
                db,
                account_id=account.id,
                run_id=run_id,
                trade_date=trade_date,
                trigger=trigger,
                source="position-t",
                code=position.code,
                name=position.name or "",
                action="deferred_buy",
                decision="wait",
                reason="已有未终结T回补委托，等待下一健康报价轮次继续撮合",
                price=price,
                amount=amount,
                candidate=ctx,
            ))
            continue

        from app.trading.service import SubmitOrderCommand, submit_order

        quote_context = _quote_round_context()
        decision_round_id = str(quote_context.get("round_id") or "")
        result = await submit_order(
            db,
            SubmitOrderCommand(
                code=position.code,
                side="buy",
                price=price,
                quantity=amount,
                broker="paper",
                account_id=account.account_name,   # 五策略并行: T回补落账到当前策略账户 (2026-08-31)
                strategy_id="paper-auto-t",
                strategy_version=_strategy_version(account.account_name),
                signal_id=(
                    f"auto-t-buyback-{decision_round_id or trade_date.strftime('%Y%m%d')}-{position.code}"
                )[:80],
                source="position-t",
                reason=reason,
                execute=execute,
                decision_round_id=decision_round_id,
                decision_at=_paper_now(),
                as_of_at=(
                    quote_context.get("as_of_at")
                    if isinstance(quote_context.get("as_of_at"), datetime)
                    else None
                ),
                idempotency_key=(
                    f"{decision_round_id}:{account.account_name}:position-t:buy:{position.code}"
                    if decision_round_id
                    else ""
                ),
                defer_until_next_round=bool(
                    settings.PAPER_DEFER_AUTO_FILL_TO_NEXT_ROUND
                ),
                deferred_metadata={
                    "candidate": ctx,
                    "confirmed_at": confirmed_at.isoformat(),
                    "block_warn": True,
                },
            ),
        )
        await _refresh_expired_paper_rows(db, account, position, *logs)
        order = result.get("order") or {}
        fills = result.get("fills") or []
        trade_id = fills[0].get("broker_trade_id") if fills else None
        order_status = str(order.get("status") or "")
        decision = (
            "executed"
            if order_status == "filled"
            else "wait"
            if order_status in {"submitted", "partial"}
            else "blocked"
        )
        logs.append(await _add_auto_log(
            db,
            account_id=account.id,
            run_id=run_id,
            trade_date=trade_date,
            trigger=trigger,
            source="position-t",
            code=position.code,
            name=position.name or "",
            action="buy" if decision == "executed" else "deferred_buy" if decision == "wait" else "skip_buy",
            decision=decision,
            reason=order.get("error_message") or reason,
            price=price,
            amount=amount,
            risk_level=risk.get("final_level"),
            risk=risk,
            candidate=ctx,
            executed_trade_id=trade_id,
        ))
        if decision == "executed":
            account = await _refresh_account(db, account)
    return logs


async def _active_highboard_queue_codes(
    db: AsyncSession,
    *,
    trade_date: date,
) -> set[str]:
    """返回策略E当日仍在涨停买一队列中的代码。"""
    from app.models.trading import TradeOrder

    rows = (
        await db.execute(
            select(TradeOrder.code).where(
                TradeOrder.broker == "paper",
                TradeOrder.account_id == PAPER_ACCOUNT_TENBAGGER,
                TradeOrder.source == "tenbagger_midline",
                TradeOrder.side == "buy",
                TradeOrder.status == "submitted",
                TradeOrder.trade_date == trade_date,
            )
        )
    ).scalars().all()
    return {str(code) for code in rows if code}


async def _has_active_paper_order(
    db: AsyncSession,
    *,
    account_name: str,
    code: str,
    side: str,
    trade_date: date,
) -> bool:
    from app.models.trading import TradeOrder

    order_id = await db.scalar(
        select(TradeOrder.id)
        .where(
            TradeOrder.broker == "paper",
            TradeOrder.account_id == account_name,
            TradeOrder.code == code,
            TradeOrder.side == side,
            TradeOrder.status.in_(("pending", "submitted", "partial")),
            TradeOrder.trade_date == trade_date,
        )
        .limit(1)
    )
    return order_id is not None


async def _active_paper_order_codes(
    db: AsyncSession,
    *,
    account_name: str,
    trade_date: date,
) -> set[str]:
    """返回账户当日所有未终结买单，防止跨轮重复创建延迟委托。"""
    from app.models.trading import TradeOrder

    rows = await db.scalars(
        select(TradeOrder.code).where(
            TradeOrder.broker == "paper",
            TradeOrder.account_id == account_name,
            TradeOrder.side == "buy",
            TradeOrder.status.in_(("pending", "submitted", "partial")),
            TradeOrder.trade_date == trade_date,
        )
    )
    return {str(code) for code in rows.all() if code}


async def _reconcile_highboard_queue_logs(
    db: AsyncSession,
    *,
    account: PaperAccount,
    run_id: str,
    trade_date: date,
    trigger: str,
) -> list[PaperAutoTradeLog]:
    """撮合/撤销E策略涨停排队单，并转换为可追踪的模拟盘日志。"""
    from app.trading.service import reconcile_paper_limit_up_orders

    results = await reconcile_paper_limit_up_orders(
        db,
        account_id=account.account_name,
    )
    logs: list[PaperAutoTradeLog] = []
    for result in results:
        event = str(result.get("event") or "")
        if event == "waiting":
            continue
        order = result.get("order") or {}
        queue = result.get("queue") or {}
        fills = result.get("fills") or []
        code = str(order.get("code") or "")
        spot = await _spot_by_code(db, code)
        name = str(order.get("name") or getattr(spot, "name", "") or code)
        candidate = {
            "_source": "tenbagger_midline",
            "limit_up_queue": True,
            "queue_order_id": order.get("order_id"),
            "queue_fill_trigger": queue.get("fill_trigger"),
            "queue_ahead_hands": queue.get("queue_ahead_hands"),
            "traded_after_queue_hands": queue.get("traded_after_queue_hands"),
            "required_hands": queue.get("required_hands"),
        }
        if event == "filled":
            fill = fills[0] if fills else {}
            fill_price = _to_float(fill.get("price")) or _to_float(order.get("avg_fill_price")) or _to_float(order.get("price"))
            amount = int(fill.get("quantity") or order.get("filled_quantity") or order.get("quantity") or 0)
            reason = str(result.get("reason") or "涨停排队委托已成交")
            trade_id = fill.get("broker_trade_id")
            logs.append(await _add_auto_log(
                db,
                account_id=account.id,
                run_id=run_id,
                trade_date=trade_date,
                trigger=trigger,
                source="tenbagger_midline",
                code=code,
                name=name,
                action="buy",
                decision="executed",
                reason=reason,
                price=fill_price,
                amount=amount,
                risk_level=(result.get("risk") or {}).get("final_level"),
                risk=result.get("risk") or {},
                candidate=candidate,
                executed_trade_id=trade_id,
            ))
            stop_loss = _to_float(queue.get("stop_loss_price"))
            if stop_loss is None and fill_price:
                stop_loss = fill_price * (1 - _highboard_policy(account.account_name)["stop_loss_pct"] / 100)
            await _apply_auto_position_risk(
                db,
                account.id,
                code,
                reason=reason,
                stop_loss_price=stop_loss,
            )
            continue

        decision = "blocked" if event == "risk_blocked" else "skipped"
        logs.append(await _add_auto_log(
            db,
            account_id=account.id,
            run_id=run_id,
            trade_date=trade_date,
            trigger=trigger,
            source="tenbagger_midline",
            code=code,
            name=name,
            action="skip_buy",
            decision=decision,
            reason=str(result.get("reason") or "涨停排队委托未成交"),
            price=_to_float(order.get("price")),
            amount=int(order.get("quantity") or 0),
            risk_level=(result.get("risk") or {}).get("final_level"),
            risk=result.get("risk") or {},
            candidate=candidate,
        ))
    return logs


async def _record_control_sample(
    db: AsyncSession,
    *,
    account: PaperAccount,
    trade_date: date,
    observed_at: datetime,
    candidates: list[dict],
    allow_empty: bool = False,
) -> PaperControlSample | None:
    """每个 Champion 每日最多落一个前向控制样本，强制样本永不计入绩效。"""
    if (
        not settings.PAPER_CONTROL_SAMPLE_ENABLED
        or account.account_name not in PAPER_CONTROL_CHALLENGER_BY_BASE
    ):
        # 控制样本只补充A/E候选审计；A2/E2已有独立交易路线，不能用控制样本当成交。
        return None
    strategy_version = _strategy_version(account.account_name)
    sample_key = (
        f"control:{trade_date.isoformat()}:{account.account_name}:{strategy_version}"
    )[:128]
    existing = await db.scalar(
        select(PaperControlSample).where(
            PaperControlSample.sample_key == sample_key
        )
    )
    if existing is not None:
        return existing
    context = _quote_round_context()
    round_id = str(context.get("round_id") or "")
    if candidates and not round_id:
        # 候选样本必须可回到不可变轮次；watchdog/手工无轮次调用不伪造证据。
        return None
    if not candidates and not allow_empty:
        return None

    candidate = dict(candidates[0]) if candidates else {}
    challenger_account_id = None
    challenger_name = PAPER_CONTROL_CHALLENGER_BY_BASE.get(account.account_name)
    if challenger_name:
        challenger = await _get_or_create_account(db, challenger_name)
        challenger_account_id = challenger.id
    code = str(candidate.get("code") or "").strip() or None
    score = _to_float(candidate.get("total_score"))
    if score is None:
        score = _to_float(candidate.get("score"))
    round_spot = _round_spot(code) if code else None
    price = next(
        (
            value
            for value in (
                _to_float(candidate.get("execution_price")),
                _to_float(candidate.get("market_price")),
                _to_float(candidate.get("price")),
                _to_float(getattr(round_spot, "price", None)),
            )
            if value is not None and value > 0
        ),
        None,
    )
    if code and price is not None:
        candidate["price"] = price
    if code and price is None:
        reason_code = "candidate_quote_missing"
        reason = "最高优先级候选缺少当前不可变轮次有效价格；仅保留失败关闭证据，不评估收益"
    elif code:
        reason_code = "top_candidate_observed"
        reason = "记录当日首个最高优先级候选，供下一轮可成交性与收盘收益对照；不参与绩效"
    else:
        reason_code = "no_candidate"
        reason = "当日截至收盘未形成可审计候选；空样本同样计入运行SLA"
    owned_snapshot = {
        **candidate,
        "_audit": {
            "quote_round_id": round_id or None,
            "as_of_at": context.get("as_of_at"),
            "component_watermarks_json": context.get("component_watermarks_json"),
            "config_version": context.get("config_version"),
            "code_version": context.get("code_version"),
            "forced_probe_enabled": bool(settings.PAPER_FORCED_PROBE_ENABLED),
        },
    }
    sample = PaperControlSample(
        sample_key=sample_key,
        account_id=account.id,
        challenger_account_id=challenger_account_id,
        trade_date=trade_date,
        strategy_version=strategy_version,
        quote_round_id=round_id or None,
        observed_at=observed_at,
        source=str(candidate.get("_source") or "") or None,
        code=code,
        name=str(candidate.get("name") or "")[:20] or None,
        price=price,
        candidate_score=score,
        decision=("observed" if code and price is not None else "invalid_quote" if code else "empty"),
        reason_code=reason_code,
        reason=reason,
        candidate_json=json.dumps(
            owned_snapshot,
            ensure_ascii=False,
            default=str,
            sort_keys=True,
        ),
        forced_probe=False,
        excluded_from_performance=True,
    )
    db.add(sample)
    await db.flush()
    return sample


async def _update_control_sample_next_round(
    db: AsyncSession,
    *,
    account: PaperAccount,
    trade_date: date,
    observed_at: datetime,
) -> int:
    context = _quote_round_context()
    current_round_id = str(context.get("round_id") or "")
    if not current_round_id:
        return 0
    samples = list(
        (
            await db.scalars(
                select(PaperControlSample).where(
                    PaperControlSample.account_id == account.id,
                    PaperControlSample.trade_date == trade_date,
                    PaperControlSample.next_round_id.is_(None),
                    PaperControlSample.code.is_not(None),
                    PaperControlSample.decision == "observed",
                )
            )
        ).all()
    )
    updated = 0
    for sample in samples:
        if str(sample.quote_round_id or "") == current_round_id:
            continue
        spot = _round_spot(str(sample.code or ""))
        quote_ok, _ = _execution_quote_status(
            spot,
            trade_date,
            now=observed_at,
        )
        if not quote_ok:
            continue
        from app.trading.service import _depth_fill_plan

        original_limit = float(sample.price or 0)
        fillable, depth_price, _ = _depth_fill_plan(
            spot,
            side="buy",
            limit_price=original_limit,
            remaining_quantity=100,
        )
        sample.next_round_id = current_round_id
        sample.next_round_price = (
            depth_price
            if depth_price is not None
            else _to_float(getattr(spot, "price", None))
        )
        sample.next_round_fillable_amount = fillable
        updated += 1
    if updated:
        await db.flush()
    return updated


async def _reconcile_deferred_order_logs(
    db: AsyncSession,
    *,
    account: PaperAccount,
    run_id: str,
    trade_date: date,
    trigger: str,
    observed_at: datetime,
) -> list[PaperAutoTradeLog]:
    from app.trading.service import reconcile_paper_deferred_orders

    context = _quote_round_context()
    outcomes = await reconcile_paper_deferred_orders(
        db,
        account_id=account.account_name,
        round_id=str(context.get("round_id") or ""),
        now=observed_at,
    )
    # A broker rejection can include an atomic rollback, expiring caller ORM state.
    # Reload before recording the actual outcome; never silently lose its audit log.
    await _refresh_expired_paper_rows(db, account)
    # Observe returned sell facts independently of the later open-position scan.
    # A filled order is not proof of a fully closed position or an exit cycle.
    execution_frames = []
    capture_execution = append_execution = None
    try:
        if type(outcomes) is not list or len(outcomes) > 256:
            raise ValueError("execution_observation_batch_invalid_or_unbounded")
        from app.paper.position_observation import (
            capture_deferred_execution_frame, append_deferred_execution_frames,
        )
        capture_execution = capture_deferred_execution_frame
        append_execution = append_deferred_execution_frames
    except Exception as exc:
        logger.warning("Deferred execution observation hook unavailable (%s)", type(exc).__name__)
    logs: list[PaperAutoTradeLog] = []
    for outcome in outcomes:
        event = str(outcome.get("event") or "")
        order = outcome.get("order") or {}
        fills = outcome.get("fills") or []
        deferred = outcome.get("deferred") or {}
        candidate = deferred.get("candidate")
        if not isinstance(candidate, dict):
            candidate = {
                "decision_round_id": order.get("decision_round_id"),
                "fill_round_id": order.get("last_fill_round_id"),
                "depth_levels": outcome.get("depth_levels") or [],
            }
        side = str(order.get("side") or "")
        code = str(order.get("code") or "")
        if side == "sell" and capture_execution is not None:
            try:
                frame = capture_execution(
                    account_id=account.id, account_name=account.account_name,
                    trade_date=trade_date, quote_context=context,
                    evaluated_at=observed_at, outcome=outcome,
                )
                if frame is not None:
                    execution_frames.append(frame)
            except Exception as exc:
                logger.warning("Deferred execution capture unavailable (%s)", type(exc).__name__)
        name, _ = await _stock_info(db, code)
        filled_amount = sum(int(item.get("quantity") or 0) for item in fills)
        filled_value = sum(
            float(item.get("price") or 0) * int(item.get("quantity") or 0)
            for item in fills
        )
        fill_price = (
            round(filled_value / filled_amount, 4)
            if filled_amount
            else _to_float(order.get("price"))
        )
        executed_trade_id = None
        if fills:
            try:
                executed_trade_id = int(fills[0].get("broker_trade_id"))
            except (TypeError, ValueError):
                executed_trade_id = None
        is_execution = event in {"filled", "partial"} and filled_amount >= 100
        is_wait = event == "waiting"
        if side == "sell":
            # 下一轮撮合沿用原触发证据，只追加本轮订单/成交事实；旧单缺失原因不补造。
            candidate = dict(candidate)
            candidate.setdefault("exit_trigger_reason", "")
            candidate["exit_trigger_basis"] = "original_decision" if candidate["exit_trigger_reason"] else "legacy_missing"
            candidate["exit_order_id"] = order.get("order_id")
            candidate["exit_order_status"] = order.get("status")
            candidate["exit_execution_status"] = (
                "partially_filled" if is_execution and event == "partial"
                else "filled" if is_execution
                else "order_pending" if is_wait else "blocked"
            )
            candidate["exit_filled_quantity_this_round"] = filled_amount
            candidate["execution_block_code"] = "" if is_execution else "order_waiting" if is_wait else "order_not_filled"
            candidate["execution_block_reason"] = "" if is_execution else str(outcome.get("reason") or order.get("error_message") or event)
        # 2026-09-17 复盘修复：卖出日志的 reason 原先只写执行管道文案
        # （"已通过风控，等待下一健康行情轮次按五档深度撮合" / "下一轮按五档深度全部成交"），
        # 真实触发原因只存在于 candidate_json.exit_trigger_reason，审计必须跨字段才可读。
        # 现把触发原因前置到 reason，结构化字段保持不变，仍以 candidate_json 为准。
        outcome_reason = str(outcome.get("reason") or order.get("error_message") or event)
        log_reason = (
            _sell_log_reason(candidate, outcome_reason) if side == "sell" else outcome_reason
        )
        logs.append(await _add_auto_log(
            db,
            account_id=account.id,
            run_id=run_id,
            trade_date=trade_date,
            trigger=trigger,
            source=str(order.get("source") or "position"),
            code=code,
            name=str(name or order.get("name") or code),
            action=(
                side
                if is_execution
                else f"deferred_{side}"
                if is_wait
                else f"skip_{side}"
            ),
            decision=(
                "executed"
                if is_execution
                else "wait"
                if is_wait
                else "skipped"
                if event == "canceled"
                else "blocked"
            ),
            reason=log_reason,
            price=fill_price,
            amount=filled_amount or int(order.get("quantity") or 0),
            risk_level=(outcome.get("risk") or {}).get("final_level"),
            risk=outcome.get("risk") or {},
            candidate=candidate,
            strategy_version=str(order.get("strategy_version") or "legacy_unversioned"),
            deferred_buy_outcome=outcome if side == "buy" else None,
            executed_trade_id=executed_trade_id,
            stage_code="fill" if is_execution else "execution",
            reason_code=f"deferred_{event or 'unknown'}",
            created_at=observed_at,
        ))
        if is_execution and side == "buy":
            stop_loss = _to_float(deferred.get("stop_loss_price"))
            await _apply_auto_position_risk(
                db,
                account.id,
                code,
                reason=str(order.get("reason") or outcome.get("reason") or ""),
                stop_loss_price=stop_loss,
            )
    if execution_frames and append_execution is not None:
        try:
            await append_execution(db, execution_frames)
        except Exception as exc:
            logger.warning("Deferred execution append unavailable (%s)", type(exc).__name__)
    return logs


async def expire_pending_paper_buys(db: AsyncSession, *, account_name: str, now: datetime) -> int:
    """No-quote watchdog: cancel expired buy remainders only, never fill or sell."""
    from app.trading.service import reconcile_paper_deferred_orders, reconcile_paper_limit_up_orders

    async with _account_auto_lock(account_name):
        account = await db.scalar(select(PaperAccount).where(
            PaperAccount.account_name == account_name, PaperAccount.status == "active"
        ).order_by(PaperAccount.id).limit(1))
        if account is None:
            return 0
        results = await reconcile_paper_deferred_orders(
            db, account_id=account_name, now=now, expire_only=True)
        results += await reconcile_paper_limit_up_orders(
            db, account_id=account_name, now=now, expire_only=True)
        run_id = f"paper-buy-expiry-{now.strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"
        for result in results:
            order = result.get("order") or {}
            metadata = result.get("deferred") or result.get("queue") or {}
            await _add_auto_log(
                db, account_id=account.id, run_id=run_id, trade_date=now.date(),
                trigger="pending-buy-expiry-watchdog", source=str(order.get("source") or ""),
                code=str(order.get("code") or ""), action="skip_buy",
                decision="skipped" if result.get("event") == "canceled" else "blocked",
                reason=str(result.get("reason") or ""),
                candidate=metadata.get("candidate") or {},
                risk=result.get("risk") or {},
                strategy_version=str(order.get("strategy_version") or "legacy_unversioned"),
                stage_code="execution", reason_code="pending_buy_expiry_guard", created_at=now,
            )
        await db.commit()
        return len(results)


async def run_paper_position_risk_monitor(
    db: AsyncSession,
    *,
    account_name: str = PAPER_ACCOUNT_DEFAULT,
    trigger: str = "quote-round-risk",
) -> dict:
    """每个健康报价轮次执行轻量持仓风控和上一轮委托撮合。"""
    async with _account_auto_lock(account_name):
        observed_at = _paper_now()
        trade_date = observed_at.date()
        run_id = (
            f"paper-risk-{observed_at.strftime('%Y%m%d%H%M%S')}-"
            f"{uuid.uuid4().hex[:6]}"
        )
        account = await _get_or_create_account(db, account_name)
        await _update_control_sample_next_round(
            db,
            account=account,
            trade_date=trade_date,
            observed_at=observed_at,
        )
        logs = await _reconcile_deferred_order_logs(
            db,
            account=account,
            run_id=run_id,
            trade_date=trade_date,
            trigger=trigger,
            observed_at=observed_at,
        )
        order_window_ok, _ = await _paper_order_window_status(observed_at)
        logs.extend(await _run_auto_sells(
            db,
            account=account,
            run_id=run_id,
            trade_date=trade_date,
            trigger=trigger,
            execute=bool(settings.PAPER_AUTO_TRADE_ENABLED and order_window_ok),
            quote_now=observed_at,
            log_holds=False,
        ))
        if _base_strategy_account(account_name) == PAPER_ACCOUNT_TENBAGGER:
            logs.extend(await _reconcile_highboard_queue_logs(
                db,
                account=account,
                run_id=run_id,
                trade_date=trade_date,
                trigger=trigger,
            ))
        await db.commit()
        return {
            "run_id": run_id,
            "trade_date": trade_date.isoformat(),
            "account_name": account_name,
            "quote_round_id": str(_quote_round_context().get("round_id") or ""),
            "summary": {
                "executed": sum(1 for item in logs if item.decision == "executed"),
                "blocked": sum(1 for item in logs if item.decision == "blocked"),
                "skipped": sum(1 for item in logs if item.decision == "skipped"),
                "wait": sum(1 for item in logs if item.decision == "wait"),
            },
            "logs": [_auto_log_payload(item) for item in logs],
        }


async def finalize_paper_daily_outcomes(
    db: AsyncSession,
    *,
    trade_date: date | None = None,
    observed_at: datetime | None = None,
) -> dict:
    """收盘固化所有 Champion/Challenger 的终态；无成交也必须有明确原因。"""
    from app.models.trading import TradeFill, TradeOrder

    finalized_at = observed_at or _paper_now()
    target_date = trade_date or finalized_at.date()
    account_names = (*PAPER_ALL_ACCOUNTS, *PAPER_CHALLENGER_ACCOUNTS)
    accounts: dict[str, PaperAccount] = {}
    for account_name in account_names:
        accounts[account_name] = await _get_or_create_account(db, account_name)

    active_orders = list(
        (
            await db.scalars(
                select(TradeOrder).where(
                    TradeOrder.broker == "paper",
                    TradeOrder.trade_date == target_date,
                    TradeOrder.status.in_(("pending", "submitted", "partial")),
                )
            )
        ).all()
    )
    for order in active_orders:
        order.status = "canceled"
        order.error_message = "收盘仍未取得满足原限价的下一轮可见深度，终态撤单"
        risk_payload = _json_loads_dict(order.risk_json)
        risk_payload["close_finalization"] = {
            "finalized_at": finalized_at,
            "reason_code": "close_unfilled",
        }
        order.risk_json = json.dumps(
            risk_payload,
            ensure_ascii=False,
            default=str,
            sort_keys=True,
        )

    for account_name in PAPER_ALL_ACCOUNTS:
        await _record_control_sample(
            db,
            account=accounts[account_name],
            trade_date=target_date,
            observed_at=finalized_at,
            candidates=[],
            allow_empty=True,
        )

    samples = list(
        (
            await db.scalars(
                select(PaperControlSample).where(
                    PaperControlSample.trade_date == target_date
                )
            )
        ).all()
    )
    sample_codes = {str(item.code) for item in samples if item.code}
    close_spots = await _spots_by_codes(db, sample_codes) if sample_codes else []
    close_by_code = {str(item.code): item for item in close_spots}
    for sample in samples:
        if sample.finalized_at is not None:
            continue
        spot = close_by_code.get(str(sample.code or ""))
        close_price = _to_float(getattr(spot, "price", None))
        sample.close_price = close_price
        if close_price is not None and sample.price and sample.price > 0:
            sample.return_pct = round((close_price / sample.price - 1) * 100, 4)
        sample.finalized_at = finalized_at

    outcomes_payload: list[dict] = []
    for account_name in account_names:
        account = accounts[account_name]
        strategy_version = _strategy_version(account_name)
        logs = list(
            (
                await db.scalars(
                    select(PaperAutoTradeLog)
                    .where(
                        PaperAutoTradeLog.account_id == account.id,
                        PaperAutoTradeLog.trade_date == target_date,
                        PaperAutoTradeLog.created_at <= finalized_at,
                        (PaperAutoTradeLog.strategy_version == strategy_version
                         if settings.PAPER_CONTINUOUS_EXPERIMENT_ENABLED else True),
                    )
                    .order_by(PaperAutoTradeLog.created_at, PaperAutoTradeLog.id)
                )
            ).all()
        )
        orders = list(
            (
                await db.scalars(
                    select(TradeOrder).where(
                        TradeOrder.broker == "paper",
                        TradeOrder.account_id == account_name,
                        TradeOrder.trade_date == target_date,
                        TradeOrder.created_at <= finalized_at,
                        (TradeOrder.strategy_version == strategy_version
                         if settings.PAPER_CONTINUOUS_EXPERIMENT_ENABLED else True),
                    )
                )
            ).all()
        )
        order_ids = [str(item.order_id) for item in orders if item.order_id]
        fills = (
            list(
                (
                    await db.scalars(
                        select(TradeFill).where(TradeFill.order_id.in_(order_ids))
                    )
                ).all()
            )
            if order_ids
            else []
        )
        sample = next(
            (
                item
                for item in samples
                if item.account_id == account.id
                or item.challenger_account_id == account.id
            ),
            None,
        )
        heartbeat_runs = {str(item.run_id) for item in logs if item.action == "scan" and item.run_id}
        scan_runs = heartbeat_runs or {str(item.run_id) for item in logs if item.run_id}
        scan_count = len(scan_runs)
        decision_count = sum(item.action != "scan" for item in logs)
        data_waits = [item for item in logs if item.stage_code == "data_gate"]
        strategy_rejects = [item for item in logs if item.stage_code == "strategy_filter"]
        submitted_count = len(orders)
        fill_count = len(fills)
        blocked_count = sum(1 for item in logs if item.decision == "blocked")
        blocked_count += sum(
            1 for item in orders if item.status in {"risk_blocked", "rejected"}
        )
        candidate_observed = bool(sample and sample.code and sample.decision == "observed")
        had_unfilled_order = bool(orders and not fills)
        has_partial_remainder = any(
            0 < int(item.filled_quantity or 0) < int(item.quantity or 0)
            for item in orders
        )
        if fill_count and has_partial_remainder:
            terminal_status = "partial_fill"
            reason_code = "partial_fill_close_canceled"
            reason = (
                f"当日记录{fill_count}笔真实模拟成交回报，未成交余量已在收盘撤单"
            )
        elif fill_count:
            terminal_status = "filled"
            reason_code = "fills_recorded"
            reason = f"当日记录{fill_count}笔真实模拟成交回报"
        elif had_unfilled_order:
            terminal_status = "order_unfilled"
            reason_code = "close_unfilled"
            reason = "当日委托经下一轮/五档撮合后仍未成交，收盘终态撤单"
        elif data_waits:
            terminal_status = "data_limited"
            reason_code = "necessary_data_wait_no_fill"
            reason = f"当日有{len(data_waits)}条必要数据等待且无成交；不能仅归因没有策略候选"
        elif blocked_count:
            terminal_status = "blocked"
            reason_code = "risk_or_execution_blocked"
            reason = f"当日有{blocked_count}条风险或执行硬门槛拦截"
        elif account_name in {
            PAPER_ACCOUNT_CHALLENGER_A,
            PAPER_ACCOUNT_CHALLENGER_E,
        } and not settings.PAPER_CONTINUOUS_EXPERIMENT_ENABLED:
            terminal_status = "control_only"
            reason_code = (
                "control_candidate_observed"
                if candidate_observed
                else "control_no_candidate"
            )
            reason = "A2/E2仅承载隔离控制样本，未授权自动委托"
        elif candidate_observed:
            terminal_status = "candidate_observed"
            reason_code = "candidate_no_order"
            reason = "记录到候选但未形成可执行委托；控制样本不计入绩效"
        elif scan_count:
            terminal_status = "no_candidate"
            reason_code = "scanned_no_candidate"
            reason = "当日扫描已运行，但没有候选通过既有Champion门禁"
        else:
            terminal_status = "not_run"
            reason_code = "no_run_evidence"
            reason = "当日未找到该账户的运行证据，需运维复核"

        round_ids = [
            str(item.quote_round_id)
            for item in logs
            if item.quote_round_id
        ] + [
            str(item.decision_round_id)
            for item in orders
            if item.decision_round_id
        ]
        details = {
            "account_name": account_name,
            "experiment": experiment_status(account_name, at=finalized_at),
            "forced_probe_enabled": bool(settings.PAPER_FORCED_PROBE_ENABLED),
            "forced_probe_excluded": True,
            "order_statuses": {
                status: sum(1 for item in orders if item.status == status)
                for status in sorted({str(item.status) for item in orders})
            },
            "control_sample_key": sample.sample_key if sample else None,
            "scan_count_basis": "scan_heartbeats" if heartbeat_runs else "decision_runs",
            "data_wait_count": len(data_waits),
            "strategy_reject_count": len(strategy_rejects),
            "data_wait_reason_codes": sorted({str(item.reason_code) for item in data_waits}),
            "scope": "account_current_execution_version" if settings.PAPER_CONTINUOUS_EXPERIMENT_ENABLED else "legacy_account_day",
        }
        outcome = await db.scalar(
            select(PaperDailyOutcome).where(
                PaperDailyOutcome.account_id == account.id,
                PaperDailyOutcome.trade_date == target_date,
                PaperDailyOutcome.strategy_version == strategy_version,
            )
        )
        if outcome is None:
            outcome = PaperDailyOutcome(
                account_id=account.id,
                trade_date=target_date,
                strategy_version=strategy_version,
            )
            db.add(outcome)
        outcome.terminal_status = terminal_status
        outcome.reason_code = reason_code
        outcome.reason = reason
        outcome.scan_count = scan_count
        outcome.decision_count = decision_count
        outcome.submitted_order_count = submitted_count
        outcome.fill_count = fill_count
        outcome.blocked_count = blocked_count
        outcome.first_round_id = round_ids[0] if round_ids else None
        outcome.last_round_id = round_ids[-1] if round_ids else None
        outcome.control_sample_id = sample.id if sample else None
        outcome.is_terminal = True
        outcome.details_json = json.dumps(
            details,
            ensure_ascii=False,
            default=str,
            sort_keys=True,
        )
        outcome.updated_at = finalized_at
        outcome.finalized_at = finalized_at
        outcomes_payload.append({
            "account_name": account_name,
            "strategy_version": strategy_version,
            "terminal_status": terminal_status,
            "reason_code": reason_code,
            "reason": reason,
            "scan_count": scan_count,
            "decision_count": decision_count,
            "submitted_order_count": submitted_count,
            "fill_count": fill_count,
            "blocked_count": blocked_count,
            "control_sample_id": sample.id if sample else None,
        })

    await db.commit()
    return {
        "trade_date": target_date.isoformat(),
        "finalized_at": finalized_at.isoformat(sep=" "),
        "accounts": outcomes_payload,
    }


async def run_paper_auto_trade(
    db: AsyncSession,
    *,
    execute: bool = True,
    trigger: str = "manual",
    max_candidates: int = 20,
    execution_mode: str = "intraday",
    account_name: str = PAPER_ACCOUNT_DEFAULT,
    include_position_risk: bool = True,
    now: Optional[datetime] = None,
) -> dict:
    """按高胜率买点候选执行模拟盘自动买卖，并记录每个决策原因.

    account_name: 双策略并行 (2026-08-31)
      - default   : 策略A 现有链路 (明日预案 + 盘中快照)
      - promotion : 策略B 晋级预测二板赛道（基准策略）
    """
    async with _account_auto_lock(account_name):
        decision_now = now or _paper_now()
        run_id = f"paper-auto-{decision_now.strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"
        trade_date = decision_now.date()
        execution_mode = execution_mode if execution_mode in {"manual", "intraday", "close"} else "manual"
        # 即使盘中时段守卫提前跳过，也必须先绑定策略账户；否则B-F的
        # skipped 日志会落成 legacy NULL，并被默认账户复盘误收。
        account = await _get_or_create_account(db, account_name)
        if execution_mode == "intraday":
            should_run, skip_reason = await _should_run_intraday_auto_trade(decision_now)
            if not should_run:
                log = await _add_auto_log(
                    db,
                    account_id=account.id,
                    run_id=run_id,
                    trade_date=trade_date,
                    trigger=trigger,
                    source="system",
                    action="empty",
                    decision="skipped",
                    reason=skip_reason,
                )
                await db.commit()
                return {
                    "run_id": run_id,
                    "execute": execute,
                    "execution_mode": execution_mode,
                    "trade_date": trade_date.isoformat(),
                    "summary": {"executed": 0, "blocked": 0, "skipped": 1, "wait": 0, "dry_run": 0},
                    "logs": [_auto_log_payload(log)],
                }

        if execution_mode == "close":
            order_window_ok, order_window_reason = False, "收盘复盘模式"
        else:
            order_window_ok, order_window_reason = await _paper_order_window_status(decision_now)
        account = await _refresh_account(db, account)
        strategy_max_daily_buys, strategy_max_positions = _strategy_buy_limits(account_name)
        intraday_buy_start = _strategy_intraday_buy_start(account_name)
        logs: list[PaperAutoTradeLog] = []
        execute_orders = execute and execution_mode != "close" and order_window_ok
        strategy_auto_order_enabled = _strategy_auto_order_enabled(account_name, now=decision_now)
        mid_session_enable_block_reason = ""
        if (
            execute_orders
            and strategy_auto_order_enabled
            and execution_mode == "intraday"
            and not experiment_active(account_name, at=decision_now)
        ):
            mid_session_enable_block_reason = await _strategy_mid_session_enable_block_reason(
                db,
                account_id=account.id,
                trade_date=trade_date,
            )
        execute_buy_orders = bool(
            execute_orders
            and strategy_auto_order_enabled
            and not mid_session_enable_block_reason
        )
        allow_buys = execution_mode != "close"
        if execute:
            allow_buys = (
                allow_buys
                and order_window_ok
                and _is_intraday_buy_window(decision_now, start_value=intraday_buy_start)
            )
        elif execution_mode == "intraday":
            allow_buys = allow_buys and _is_intraday_buy_window(
                decision_now,
                start_value=intraday_buy_start,
            )

        if settings.PAPER_CONTINUOUS_EXPERIMENT_ENABLED and execution_mode == "intraday":
            # 独立于候选/成交计数；只证明本轮进入扫描，不宣称策略和风控已通过。
            await _add_auto_log(
                db, account_id=account.id, run_id=run_id, trade_date=trade_date,
                trigger=trigger, source="system", action="scan", decision="observed",
                reason="本轮进入账户扫描；运行开关与买入尝试窗口独立留证，不代表可成交",
                candidate={
                    "scan_heartbeat": True, "runtime_evidence_version": "account_scan_v1",
                    "global_auto_enabled": bool(settings.PAPER_AUTO_TRADE_ENABLED),
                    "account_auto_buy_enabled": bool(strategy_auto_order_enabled),
                    "order_window_open": bool(order_window_ok),
                    "buy_attempt_allowed": bool(settings.PAPER_AUTO_TRADE_ENABLED
                        and execute_buy_orders and allow_buys and account.status == "active"),
                },
                stage_code="runtime_scan", reason_code="account_scan_entered",
                created_at=decision_now,
            )

        if not settings.PAPER_AUTO_TRADE_ENABLED and execute_orders:
            logs.append(await _add_auto_log(
                db,
                account_id=account.id,
                run_id=run_id,
                trade_date=trade_date,
                trigger=trigger,
                source="system",
                action="empty",
                decision="skipped",
                reason="自动模拟交易开关未启用",
            ))
            await db.commit()
            return {"run_id": run_id, "execute": execute, "logs": [_auto_log_payload(item) for item in logs]}

        if execute_orders and not strategy_auto_order_enabled:
            logs.append(await _add_auto_log(
                db,
                account_id=account.id,
                run_id=run_id,
                trade_date=trade_date,
                trigger=trigger,
                source="system",
                action="empty",
                decision="dry_run",
                reason=(
                    f"持续实验尚未到启动日{settings.PAPER_EXPERIMENT_START_DATE}；仅观察及管理退出，不补历史单"
                    if settings.PAPER_CONTINUOUS_EXPERIMENT_ENABLED
                    else "该策略自动买入委托已因校正证据暂停；本次仍生成候选审计并继续执行持仓卖出风控"
                ),
            ))
        elif mid_session_enable_block_reason:
            logs.append(await _add_auto_log(
                db,
                account_id=account.id,
                run_id=run_id,
                trade_date=trade_date,
                trigger=trigger,
                source="system",
                action="empty",
                decision="dry_run",
                reason=mid_session_enable_block_reason,
            ))

        if execute and execution_mode != "close" and not order_window_ok:
            logs.append(await _add_auto_log(
                db,
                account_id=account.id,
                run_id=run_id,
                trade_date=trade_date,
                trigger=trigger,
                source="system",
                action="empty",
                decision="skipped",
                reason=f"{order_window_reason}，本次只做持仓检查，不发送委托",
            ))

        if include_position_risk:
            logs.extend(await _run_auto_sells(
                db,
                account=account,
                run_id=run_id,
                trade_date=trade_date,
                trigger=trigger,
                execute=execute_orders,
                quote_now=decision_now,
            ))
            account = await _refresh_account(db, account)
        if allow_buys and _base_strategy_account(account_name) not in {PAPER_ACCOUNT_TENBAGGER, PAPER_ACCOUNT_REVERSAL}:
            logs.extend(await _run_auto_t_buybacks(
                db,
                account=account,
                run_id=run_id,
                trade_date=trade_date,
                trigger=trigger,
                execute=execute_buy_orders,
            ))
            account = await _refresh_account(db, account)
        if (
            _base_strategy_account(account_name) == PAPER_ACCOUNT_TENBAGGER
            and execute
            and execution_mode != "close"
        ):
            logs.extend(await _reconcile_highboard_queue_logs(
                db,
                account=account,
                run_id=run_id,
                trade_date=trade_date,
                trigger=trigger,
            ))
            account = await _refresh_account(db, account)
        open_positions = await _open_positions(db, account.id)
        held_codes = {item.code for item in open_positions}
        position_by_code = {item.code: item for item in open_positions}
        allow_profit_position_recovery = _has_recovery_protected_profit_position(open_positions)
        today_new_buy_logs = await _today_auto_new_buy_logs(
            db,
            trade_date,
            account_id=account.id,
            include_legacy_null=account_name == PAPER_ACCOUNT_DEFAULT,
        )
        today_new_buy_count = len(today_new_buy_logs)
        today_bought_codes = {str(item.code) for item in today_new_buy_logs if item.code}
        queued_codes = await _active_paper_order_codes(
            db,
            account_name=account.account_name,
            trade_date=trade_date,
        )
        market_sentiment = await _market_sentiment_for_date(db, trade_date)
        strong_market_recovery_day = _is_strong_market_recovery_sentiment(market_sentiment)
        include_daily_participation = (
            today_new_buy_count == 0
            and _is_daily_participation_time(decision_now)
            and _market_allows_daily_participation(market_sentiment)
        )
        include_icepoint_reversal = (
            today_new_buy_count == 0
            and _is_icepoint_reversal_time(decision_now)
            and _market_is_icepoint_reversal_setup(market_sentiment)
        )
        today_sector_counts = _sector_counts_from_auto_logs(today_new_buy_logs)
        run_sector_counts: dict[str, int] = {}
        candidates: list[dict] = []
        candidate_notes: list[str] = []
        candidate_diagnostics: list[dict] = []
        if allow_buys:
            if account_name in PAPER_PROMOTION_ACCOUNTS:
                # 策略B/C/D: 晋级预测各赛道（基准策略）
                candidates, candidate_notes = await _promotion_route_buy_candidates(
                    db,
                    limit=max_candidates,
                    trade_date=trade_date,
                    account_name=account_name,
                    diagnostics=candidate_diagnostics,
                    now=decision_now,
                )
            elif _base_strategy_account(account_name) == PAPER_ACCOUNT_TENBAGGER:
                # E低位入口与E2强势/回封入口共享质量和成交硬约束，独立账户配对实验。
                candidates, candidate_notes = await _tenbagger_midline_candidates(
                    db,
                    limit=max_candidates,
                    trade_date=trade_date,
                    **({"account_name": account_name} if account_name == PAPER_ACCOUNT_CHALLENGER_E else {}),
                    diagnostics=candidate_diagnostics,
                )
            elif account_name == PAPER_ACCOUNT_REVERSAL:
                # 策略F: 断板反包 (连板≥3深跌断板后放量反包, 2026-08-31 晚新增)
                candidates, candidate_notes = await _reversal_pullback_candidates(
                    db,
                    limit=max_candidates,
                    trade_date=trade_date,
                    diagnostics=candidate_diagnostics,
                )
            else:
                # 策略A: 现有链路 (明日预案 + 盘中快照)
                candidates, candidate_notes = await _paper_auto_buy_candidates(
                    db,
                    limit=max_candidates,
                    trade_date=trade_date,
                    account_id=account.id,
                    include_daily_participation=include_daily_participation,
                    include_icepoint_reversal=include_icepoint_reversal,
                )
        await _record_control_sample(
            db,
            account=account,
            trade_date=trade_date,
            observed_at=decision_now,
            candidates=candidates,
        )
        bought = 0
        dry_run_new_codes: set[str] = set()

        # 结构化原因逐个落账，不受三条展示摘要截断；数据等待与策略拒绝分开。
        for diagnostic in candidate_diagnostics:
            logs.append(await _add_auto_log(
                db, run_id=run_id, trade_date=trade_date, trigger=trigger,
                source="candidate", action="candidate_reject", decision="wait",
                account_id=account.id, **diagnostic,
            ))
        for note in candidate_notes[:3]:
            logs.append(await _add_auto_log(
                db,
                run_id=run_id,
                trade_date=trade_date,
                trigger=trigger,
                source="candidate",
                action="empty",
                decision="wait",
                reason=note,
                account_id=account.id,
            ))

        if allow_buys and not candidates:
            empty_reason = _strategy_empty_reason(account_name)
            logs.append(await _add_auto_log(
                db,
                run_id=run_id,
                trade_date=trade_date,
                trigger=trigger,
                source="candidate",
                action="empty",
                decision="wait",
                reason=empty_reason,
                account_id=account.id,
            ))

        if not allow_buys:
            reason = "收盘复盘模式不再自动买入，只做持仓检查与演练记录"
            if execution_mode == "intraday" and order_window_ok:
                reason = (
                    f"当前不在盘中买入窗口"
                    f"({intraday_buy_start}-{settings.PAPER_INTRADAY_BUY_END})"
                )
            elif execution_mode != "close" and order_window_reason:
                reason = f"{order_window_reason}，自动买入跳过"
            logs.append(await _add_auto_log(
                db,
                account_id=account.id,
                run_id=run_id,
                trade_date=trade_date,
                trigger=trigger,
                source="system",
                action="skip_buy",
                decision="skipped",
                reason=reason,
            ))

        for candidate in candidates:
            if not allow_buys:
                break
            code = str(candidate.get("code") or "").strip()
            if not code:
                continue
            source = str(candidate.get("_source") or "candidate")
            if account_name == PAPER_ACCOUNT_DEFAULT and execution_mode == "intraday":
                # SAVEPOINT-isolated observation; audit errors return unknown and never feed execution_confirmation.
                candidate["confirmation_evidence"] = await historical_confirmation_evidence(
                    db, account_id=account.id, trade_date=trade_date, code=code,
                    source=source, strategy_version=_strategy_version(account_name),
                    observed_at=decision_now,
                )
            if code in queued_codes:
                # 已有同代码涨停排队单，禁止每分钟重复报单。
                continue
            spot = await _spot_by_code(db, code)
            display_name = (
                str(getattr(spot, "name", "") or "")
                or str(candidate.get("name") or "")
            )
            score = float(candidate.get("total_score") or 0)
            if not score:
                score = float(candidate.get("score") or 0)
            quote_ok, quote_reason = _execution_quote_status(spot, trade_date)
            if not quote_ok:
                logs.append(await _add_auto_log(
                    db,
                    account_id=account.id,
                    run_id=run_id,
                    trade_date=trade_date,
                    trigger=trigger,
                    source=source,
                    code=code,
                    name=display_name,
                    action="skip_buy",
                    decision="blocked",
                    reason=f"买入行情硬门槛：{quote_reason}",
                    price=_to_float(getattr(spot, "price", None)) if spot else None,
                    candidate_score=score,
                    candidate=candidate,
                ))
                continue
            market_price = float(spot.price)
            fund_reject = await _confirm_candidate_main_fund(
                db, candidate, spot, trade_date=trade_date, decision_at=decision_now,
            )
            if fund_reject:
                candidate["execution_confirmation"] = False
                if "confirmation_evidence" in candidate:
                    candidate["confirmation_evidence"]["current_setup_valid"] = "false"
                logs.append(await _add_auto_log(
                    db, account_id=account.id, run_id=run_id, trade_date=trade_date,
                    trigger=trigger, source=source, code=code, name=display_name,
                    action="skip_buy", decision="blocked", reason=fund_reject,
                    price=market_price, candidate_score=score, candidate=candidate,
                    stage_code="data_gate", reason_code="main_fund_not_confirmed",
                ))
                continue
            if execution_mode == "intraday":
                if account_name == PAPER_ACCOUNT_DEFAULT:
                    price_band = _a_entry_price_band(spot)
                    candidate["entry_price_band"] = price_band
                    if price_band["status"] == "empty":
                        candidate["confirmation_evidence"]["current_setup_valid"] = "false"
                        logs.append(await _add_auto_log(
                            db, account_id=account.id, run_id=run_id,
                            trade_date=trade_date, trigger=trigger, source=source,
                            code=code, name=display_name, action="skip_buy", decision="skipped",
                            reason=(
                                f"A策略必要价格区间为空：高点保持要求≥{price_band['lower_price']:.4f}，"
                                f"低点反弹上限要求≤{price_band['upper_price']:.4f}；"
                                "当前振幅不适合该低吸规则，不是T+1禁买或账户暂停"
                            ),
                            price=market_price, candidate_score=score, candidate=candidate,
                            stage_code="strategy_filter", reason_code="entry_constraint_empty",
                            metric_value=price_band["lower_price"],
                            threshold_value=price_band["upper_price"],
                        ))
                        continue
                confirmation_policy = account_confirmation_policy(account_name)
                stable_ok, stable_reason, stable_metrics = _stable_intraday_entry_quote(spot, account_name=account_name)
                if not stable_ok:
                    if "confirmation_evidence" in candidate:
                        candidate["confirmation_evidence"]["current_setup_valid"] = "false"
                    logs.append(await _add_auto_log(
                        db,
                        account_id=account.id,
                        run_id=run_id,
                        trade_date=trade_date,
                        trigger=trigger,
                        source=source,
                        code=code,
                        name=display_name,
                        action="skip_buy",
                        decision="blocked",
                        reason=f"入场分时策略过滤：{stable_reason}",
                        price=market_price,
                        candidate_score=score,
                        candidate=candidate,
                    ))
                    continue
                sample_at = getattr(spot, "updated_at", None)
                if not isinstance(sample_at, datetime):
                    sample_at = decision_now
                candidate.update(stable_metrics)
                candidate["confirmation_version"] = "champion_persistent_v1"
                candidate["confirmation_sample_at"] = sample_at.isoformat()
                ready, sample_count, persistence_sec = (
                    await _champion_intraday_confirmation_status(
                        db,
                        account_id=account.id,
                        trade_date=trade_date,
                        code=code,
                        source=source,
                        current_at=sample_at,
                        account_name=account_name,
                    )
                )
                if ready and "confirmation_evidence" in candidate and candidate["confirmation_evidence"]["historical_quote_path_confirmed"] != "true":
                    candidate["confirmation_evidence"].update(
                        historical_quote_path_confirmed="true",
                        historical_confirmed_at=decision_now.isoformat(),
                    )
                candidate["confirmation_sample_count"] = sample_count
                candidate["confirmation_persistence_sec"] = round(persistence_sec, 1)
                logs.append(await _add_auto_log(
                    db,
                    account_id=account.id,
                    run_id=run_id,
                    trade_date=trade_date,
                    trigger=trigger,
                    source=source,
                    code=code,
                    name=display_name,
                    action="confirm_buy",
                    # 这里只表示报价路径通过，不代表策略、风控或委托已就绪。
                    decision="quote_confirmed" if ready else "wait",
                    reason=(
                        f"报价路径确认完成（非下单确认）：{sample_count}帧/{persistence_sec:.0f}秒"
                        if ready
                        else (
                            f"等待报价路径确认：当前{sample_count}帧/{persistence_sec:.0f}秒，"
                            f"要求至少{confirmation_policy['min_samples']}帧/"
                            f"{confirmation_policy['min_persistence_sec']}秒"
                        )
                    ),
                    price=market_price,
                    candidate_score=score,
                    candidate=candidate,
                    stage_code="quote_confirmation",
                    reason_code="quote_path_ready" if ready else "confirmation_wait",
                    metric_value=persistence_sec,
                    threshold_value=float(confirmation_policy["min_persistence_sec"]),
                ))
                if not ready:
                    continue
            limit_up_queue_order = bool(
                _base_strategy_account(account_name) == PAPER_ACCOUNT_TENBAGGER
                and candidate.get("limit_up_queue")
                and _is_limit_up_queue_quote(spot)
            )
            price = (
                _to_float(getattr(spot, "limit_up", None))
                if limit_up_queue_order
                else _conservative_execution_price(spot, "buy")
            )
            if price is None:
                logs.append(await _add_auto_log(
                    db,
                    account_id=account.id,
                    run_id=run_id,
                    trade_date=trade_date,
                    trigger=trigger,
                    source=source,
                    code=code,
                    name=display_name,
                    action="skip_buy",
                    decision="blocked",
                    reason="买入盘口不可成交；非E策略排队条件或真一字板禁止模拟成交",
                    price=market_price,
                    candidate_score=score,
                    candidate=candidate,
                ))
                continue
            candidate["market_price"] = market_price
            candidate["execution_price"] = price
            candidate["limit_up_queue_order"] = limit_up_queue_order
            candidate["quote_updated_at"] = getattr(spot, "updated_at", None)
            stop_loss = _candidate_stop_loss(candidate, price)
            candidate["stop_loss_price"] = stop_loss
            # 策略B/C/D/E/F均有各自经治理或回放确定的入场闸门，不能再继承策略A的低吸闸门。
            if (
                str(candidate.get("_source") or "").startswith("promotion_")
                or candidate.get("_source") in {"tenbagger_midline", "reversal_pullback"}
            ):
                value_reject_reason = ""
            else:
                value_reject_reason = _candidate_execution_value_reject_reason(
                    candidate,
                    price=price,
                    spot=spot,
                )
            if value_reject_reason:
                if "confirmation_evidence" in candidate:
                    candidate["confirmation_evidence"]["current_setup_valid"] = "false"
                logs.append(await _add_auto_log(
                    db,
                    account_id=account.id,
                    run_id=run_id,
                    trade_date=trade_date,
                    trigger=trigger,
                    source=source,
                    code=code,
                    name=display_name,
                    action="skip_buy",
                    decision="skipped",
                    reason=f"性价比闸门：{value_reject_reason}",
                    price=price,
                    candidate_score=score,
                    candidate=candidate,
                ))
                continue

            continuous_participation_probe = (
                not experiment_active(account_name, at=decision_now)
                and _current_account_drawdown(account)
                >= settings.PAPER_AUTO_DRAWDOWN_RECOVERY_HARD_PAUSE_PCT
                and _candidate_allows_continuous_participation(candidate, score)
            )
            open_count = len(held_codes | dry_run_new_codes) + len(
                queued_codes - held_codes - dry_run_new_codes
            )
            current_now = decision_now
            if _is_late_new_buy_time(current_now) and not _candidate_allows_late_new_buy(current_now, market_sentiment, source, score):
                logs.append(await _add_auto_log(
                    db,
                    account_id=account.id,
                    run_id=run_id,
                    trade_date=trade_date,
                    trigger=trigger,
                    source=source,
                    code=code,
                    name=display_name,
                    action="skip_buy",
                    decision="skipped",
                    reason=(
                        f"短线尾盘{settings.PAPER_AUTO_LATE_NEW_BUY_CUTOFF}后不新开仓，"
                        "避免买入后受T+1锁仓"
                    ),
                    price=price or None,
                    candidate_score=score,
                    candidate=candidate,
                ))
                continue
            # 午后“强势市场才开仓”是策略A低吸/异动候选的组合风控，不能覆盖
            # B-F各自经治理或历史回放确定的独立入场条件；尤其C需要消费13:05快照，
            # E需要允许早盘曾开板后回封的高标在14:00前提交真实排队单。
            if (
                account_name == PAPER_ACCOUNT_DEFAULT
                and not experiment_active(account_name, at=current_now)
                and _is_afternoon_new_buy_time(current_now)
                and not _candidate_allows_afternoon_new_buy(market_sentiment, source, score)
            ):
                logs.append(await _add_auto_log(
                    db,
                    account_id=account.id,
                    run_id=run_id,
                    trade_date=trade_date,
                    trigger=trigger,
                    source=source,
                    code=code,
                    name=display_name,
                    action="skip_buy",
                    decision="skipped",
                    reason=(
                        "午后未满足强势市场或99分个股龙头豁免，不新开仓："
                        f"涨停{int(getattr(market_sentiment, 'limit_up_count', 0) or 0)}家，"
                        f"跌停{int(getattr(market_sentiment, 'limit_down_count', 0) or 0)}家，"
                        f"封板率{float(getattr(market_sentiment, 'seal_rate', 0) or 0):.1f}%，"
                        f"涨跌比{float(getattr(market_sentiment, 'advance_decline_ratio', 0) or 0):.2f}，"
                        f"主力净流入{float(getattr(market_sentiment, 'main_net_inflow', 0) or 0):.2f}亿"
                    ),
                    price=price or None,
                    candidate_score=score,
                    candidate=candidate,
                ))
                continue
            # 入场策略确认与账户执行约束分层：满仓/资金不足也保留真实买点。
            # A延续性过滤提前只读计算一次，后续原交易分支复用，不放宽规则。
            continuation_reason = "" if account_name != PAPER_ACCOUNT_DEFAULT else await _continuation_risk_reject_reason(
                db, code=code, trade_date=trade_date, candidate=candidate,
            )
            if "confirmation_evidence" in candidate:
                candidate["confirmation_evidence"]["current_setup_valid"] = "false" if continuation_reason else "true"
            if execute_buy_orders and execution_mode == "intraday" and not continuation_reason:
                from app.push.paper_buy_points import record_buy_point, candidate_reason
                signal_context = _quote_round_context()
                signal_kind = str(
                    candidate.get("entry_variant") or candidate.get("buy_point_type")
                    or candidate.get("strategy_label") or source
                )
                await record_buy_point(
                    db, account=account, strategy_version=_strategy_version(account_name),
                    label=_strategy_display_meta(account)["label"], code=code, name=display_name,
                    source=source, signal_key=signal_kind,
                    reason=candidate_reason(candidate, _candidate_buy_reason(candidate, score, stop_loss)),
                    price=market_price, observed_at=decision_now, decision_run_id=run_id,
                    quote_round_id=str(signal_context.get("round_id") or ""),
                    as_of_at=signal_context.get("as_of_at"),
                    code_version=str(signal_context.get("code_version") or ""),
                    queue_order=limit_up_queue_order,
                    market_context={
                        **{key: getattr(spot, key, None) for key in (
                            "price", "prev_close", "open", "high", "low", "change_pct",
                            "avg_price", "volume_ratio", "turnover_rate", "amount",
                            "circ_market_cap", "limit_up", "limit_down",
                            "orderbook_imbalance", "bid_ask_spread",
                        )},
                        "source_quote_at": str(getattr(spot, "source_quote_at", "") or ""),
                        "quote_round_id": str(getattr(spot, "quote_round_id", "") or ""),
                        "stop_loss_price": stop_loss,
                        "confirmation_sample_count": candidate.get("confirmation_sample_count"),
                        "confirmation_persistence_sec": candidate.get("confirmation_persistence_sec"),
                    },
                )
            high_conviction_strong_market_recovery = (
                strong_market_recovery_day
                and score >= settings.PAPER_AUTO_DRAWDOWN_HARD_CONVICTION_MIN_SCORE
            )
            recovery_max_buys = (
                settings.PAPER_AUTO_STRONG_MARKET_RECOVERY_MAX_BUYS
                if high_conviction_strong_market_recovery
                else settings.PAPER_AUTO_DRAWDOWN_RECOVERY_MAX_BUYS
            )
            recovery_max_open_positions = (
                settings.PAPER_AUTO_STRONG_MARKET_RECOVERY_MAX_OPEN_POSITIONS
                if high_conviction_strong_market_recovery
                else settings.PAPER_AUTO_DRAWDOWN_RECOVERY_MAX_OPEN_POSITIONS
            )
            recovery_buy = _is_drawdown_recovery_buy(
                account,
                open_count=open_count,
                today_new_buy_count=today_new_buy_count + bought + len(queued_codes),
                candidate_score=score,
                allow_profit_position=allow_profit_position_recovery and _is_recovery_buy_time_window(current_now),
                recovery_max_buys=recovery_max_buys,
                recovery_max_open_positions=recovery_max_open_positions,
                allow_continuous_participation=continuous_participation_probe,
            )
            pause_reason = _auto_buy_pause_reason(
                account,
                open_count=open_count,
                today_new_buy_count=today_new_buy_count + bought + len(queued_codes),
                candidate_score=score,
                allow_profit_position=allow_profit_position_recovery and _is_recovery_buy_time_window(current_now),
                recovery_max_buys=recovery_max_buys,
                recovery_max_open_positions=recovery_max_open_positions,
                allow_continuous_participation=continuous_participation_probe,
            )
            if pause_reason:
                logs.append(await _add_auto_log(
                    db,
                    account_id=account.id,
                    run_id=run_id,
                    trade_date=trade_date,
                    trigger=trigger,
                    source=source,
                    code=code,
                    name=display_name,
                    action="skip_buy",
                    decision="skipped",
                    reason=pause_reason,
                    price=price or None,
                    candidate_score=score,
                    candidate=candidate,
                ))
                continue
            if today_new_buy_count + bought + len(queued_codes) >= strategy_max_daily_buys:
                if execute_buy_orders:
                    capacity_reason = (
                        f"当前账户今日真实新开仓{today_new_buy_count + bought}只、"
                        f"待成交委托{len(queued_codes)}只，合计达到策略日限"
                        f"{strategy_max_daily_buys}只，保留节奏不继续追买"
                    )
                else:
                    capacity_reason = (
                        f"演练容量已满：当前账户今日真实新开仓{today_new_buy_count}只、"
                        f"本轮演练候选{bought}只、待成交委托{len(queued_codes)}只，"
                        f"合计达到策略日限{strategy_max_daily_buys}只；演练候选未成交"
                    )
                logs.append(await _add_auto_log(
                    db,
                    account_id=account.id,
                    run_id=run_id,
                    trade_date=trade_date,
                    trigger=trigger,
                    source=source,
                    code=code,
                    name=display_name,
                    action="skip_buy",
                    decision="skipped",
                    reason=capacity_reason,
                    price=price or None,
                    candidate_score=score,
                    candidate=candidate,
                ))
                continue
            sector_key = _candidate_sector_key(candidate)
            if (
                sector_key
                and (
                    today_sector_counts.get(sector_key, 0)
                    + run_sector_counts.get(sector_key, 0)
                ) >= settings.PAPER_AUTO_MAX_DAILY_BUYS_PER_SECTOR
            ):
                logs.append(await _add_auto_log(
                    db,
                    account_id=account.id,
                    run_id=run_id,
                    trade_date=trade_date,
                    trigger=trigger,
                    source=source,
                    code=code,
                    name=display_name,
                    action="skip_buy",
                    decision="skipped",
                    reason=f"短线同题材{sector_key}今日已有开仓，避免同主线重复打点",
                    price=price or None,
                    candidate_score=score,
                    candidate=candidate,
                ))
                continue
            existing_position = position_by_code.get(code)
            scale_in = False
            if existing_position:
                current_strategy_version = _strategy_version(account.account_name)
                position_strategy_version = str(
                    getattr(existing_position, "strategy_version", "") or ""
                )
                if position_strategy_version != current_strategy_version:
                    logs.append(await _add_auto_log(
                        db,
                        account_id=account.id,
                        run_id=run_id,
                        trade_date=trade_date,
                        trigger=trigger,
                        source=source,
                        code=code,
                        name=display_name,
                        action="skip_buy",
                        decision="blocked",
                        reason=(
                            "禁止跨策略版本分批加仓："
                            f"持仓版本={position_strategy_version or 'legacy_unversioned'}，"
                            f"当前版本={current_strategy_version}"
                        ),
                        price=price or None,
                        candidate_score=score,
                        candidate=candidate,
                    ))
                    continue
                scale_in_reason = _scale_in_reject_reason(
                    candidate,
                    position=existing_position,
                    price=price,
                    score=score,
                    total_assets=float(account.total_assets or account.initial_capital or 0),
                    bought_code_today=code in today_bought_codes,
                )
                if scale_in_reason:
                    logs.append(await _add_auto_log(
                        db,
                        account_id=account.id,
                        run_id=run_id,
                        trade_date=trade_date,
                        trigger=trigger,
                        source=source,
                        code=code,
                        name=display_name,
                        action="skip_buy",
                        decision="skipped",
                        reason=f"分批建仓：{scale_in_reason}",
                        price=price or None,
                        candidate_score=score,
                        candidate=candidate,
                    ))
                    continue
                scale_in = True
                candidate["scale_in"] = True
                candidate["position_cost"] = round(float(existing_position.buy_price or 0), 2)
            if not scale_in and open_count >= strategy_max_positions:
                if execute_buy_orders:
                    position_capacity_reason = (
                        f"当前账户持仓及待成交委托合计{open_count}只，"
                        f"已达到策略最大持仓数{strategy_max_positions}只"
                    )
                else:
                    position_capacity_reason = (
                        f"演练持仓容量已满：当前账户真实持仓{len(held_codes)}只、"
                        f"待成交委托{len(queued_codes - held_codes)}只、"
                        f"本轮新增演练候选{len(dry_run_new_codes)}只，"
                        f"按策略上限{strategy_max_positions}只不再追加；演练候选未成交"
                    )
                logs.append(await _add_auto_log(
                    db,
                    account_id=account.id,
                    run_id=run_id,
                    trade_date=trade_date,
                    trigger=trigger,
                    source=source,
                    code=code,
                    name=display_name,
                    action="skip_buy",
                    decision="skipped",
                    reason=position_capacity_reason,
                    price=price or None,
                    candidate_score=score,
                    candidate=candidate,
                ))
                continue
            if continuation_reason:
                logs.append(await _add_auto_log(
                    db,
                    account_id=account.id,
                    run_id=run_id,
                    trade_date=trade_date,
                    trigger=trigger,
                    source=source,
                    code=code,
                    name=display_name,
                    action="skip_buy",
                    decision="skipped",
                    reason=continuation_reason,
                    price=price,
                    candidate_score=score,
                    candidate=candidate,
                ))
                continue

            full_conviction = _is_full_conviction_candidate(candidate, score)
            cand_source = str(candidate.get("_source") or "")
            if cand_source.startswith("promotion_"):
                # 策略B/C/D: 晋级预测各赛道独立仓位 (单笔 ≤ 该赛道仓位上限), 不参与策略A分层
                promo_pct = {
                    PAPER_ACCOUNT_PROMOTION: settings.PAPER_PROMOTION_POSITION_PCT,
                    PAPER_ACCOUNT_MAINLINE: settings.PAPER_MAINLINE_POSITION_PCT,
                    PAPER_ACCOUNT_AUCTION: settings.PAPER_AUCTION_POSITION_PCT,
                }.get(account_name, settings.PAPER_PROMOTION_POSITION_PCT)
                amount = _round_lot(
                    float(account.total_assets or account.initial_capital or 0)
                    * promo_pct
                    * 0.98
                    // max(price, 0.01)
                )
            elif cand_source == "tenbagger_midline":
                # 策略E: 中线独立仓位 (单笔 ≤ 总资产15%), 不参与策略A分层
                amount = _round_lot(
                    float(account.total_assets or account.initial_capital or 0)
                    * _highboard_policy(account_name)["position_pct"]
                    * 0.98
                    // max(price, 0.01)
                )
            elif cand_source == "reversal_pullback":
                # 策略F: 断板反包独立仓位 (单笔 ≤ 总资产10%, 小仓位观察), 不参与策略A分层
                amount = _round_lot(
                    float(account.total_assets or account.initial_capital or 0)
                    * settings.PAPER_REVERSAL_POSITION_PCT
                    * 0.98
                    // max(price, 0.01)
                )
            else:
                amount = _layered_auto_buy_amount(
                    account,
                    price=price,
                    open_count=open_count,
                    score=score,
                    position=existing_position if scale_in else None,
                )
            # 每一种自动买点都先执行一层，避免高分、急拉或单一形态直接放大成重仓。
            # 两个预算任一不足一手都不绕过，防止分层逻辑反向穿透现金/回撤约束。
            if candidate.get("leader_first_move"):
                leader_cap = max(100, _round_lot(settings.PAPER_AUTO_GREEN_REVERSAL_LEADER_MAX_BUY_AMOUNT))
                amount = min(amount, leader_cap)
            if candidate.get("daily_participation") or continuous_participation_probe or scale_in:
                if candidate.get("daily_participation"):
                    amount = min(
                        amount,
                        _round_lot(settings.PAPER_AUTO_DAILY_PARTICIPATION_MAX_BUY_AMOUNT),
                    )
                if continuous_participation_probe:
                    amount = min(
                        amount,
                        _round_lot(settings.PAPER_AUTO_DRAWDOWN_CONTINUOUS_PARTICIPATION_MAX_AMOUNT),
                    )
            if candidate.get("icepoint_reversal"):
                icepoint_cap = max(100, _round_lot(settings.PAPER_AUTO_ICEPOINT_REVERSAL_MAX_BUY_AMOUNT))
                amount = min(amount, icepoint_cap)
            if candidate.get("ma5_pullback"):
                pullback_cap = max(100, _round_lot(settings.PAPER_AUTO_MA5_PULLBACK_MAX_BUY_AMOUNT))
                amount = min(amount, pullback_cap)
            if cand_source.startswith("promotion_"):
                promo_cap = _round_lot(
                    float(account.total_assets or account.initial_capital or 0)
                    * {
                        PAPER_ACCOUNT_PROMOTION: settings.PAPER_PROMOTION_POSITION_PCT,
                        PAPER_ACCOUNT_MAINLINE: settings.PAPER_MAINLINE_POSITION_PCT,
                        PAPER_ACCOUNT_AUCTION: settings.PAPER_AUCTION_POSITION_PCT,
                    }.get(account_name, settings.PAPER_PROMOTION_POSITION_PCT)
                    / max(price, 0.01)
                )
                amount = min(amount, max(100, promo_cap))
            if cand_source == "tenbagger_midline":
                mid_cap = _round_lot(
                    float(account.total_assets or account.initial_capital or 0)
                    * _highboard_policy(account_name)["position_pct"]
                    / max(price, 0.01)
                )
                amount = min(amount, max(100, mid_cap))
            if cand_source == "reversal_pullback":
                rev_cap = _round_lot(
                    float(account.total_assets or account.initial_capital or 0)
                    * settings.PAPER_REVERSAL_POSITION_PCT
                    / max(price, 0.01)
                )
                amount = min(amount, max(100, rev_cap))
            if amount < 100:
                logs.append(await _add_auto_log(
                    db,
                    account_id=account.id,
                    run_id=run_id,
                    trade_date=trade_date,
                    trigger=trigger,
                    source=source,
                    code=code,
                    name=display_name,
                    action="skip_buy",
                    decision="skipped",
                    reason="当前分层预算、单股仓位余量或可用资金不足以买入一手",
                    price=price,
                    amount=amount,
                    candidate_score=score,
                    candidate=candidate,
                ))
                continue

            risk = await _risk_check_for_buy(
                db,
                account,
                code,
                price,
                amount,
                recovery_probe=recovery_buy,
                continuous_participation_probe=continuous_participation_probe,
            )
            if risk.get("final_level") == "block":
                reason = "；".join(item.get("message", "") for item in risk.get("block_reasons", [])) or "风控拦截"
                logs.append(await _add_auto_log(
                    db,
                    account_id=account.id,
                    run_id=run_id,
                    trade_date=trade_date,
                    trigger=trigger,
                    source=source,
                    code=code,
                    name=display_name,
                    action="skip_buy",
                    decision="blocked",
                    reason=reason,
                    price=price,
                    amount=amount,
                    candidate_score=score,
                    risk_level=risk.get("final_level"),
                    risk=risk,
                    candidate=candidate,
                ))
                continue
            if (
                execute_buy_orders
                and settings.PAPER_AUTO_WARN_RISK_BLOCK_BUY
                and risk.get("final_level") == "warn"
                and not _continuous_participation_warn_allowed(
                    risk,
                    continuous_participation_probe,
                )
            ):
                warning_reason = "；".join(item.get("message", "") for item in risk.get("warnings", [])) or "风控警告"
                logs.append(await _add_auto_log(
                    db,
                    account_id=account.id,
                    run_id=run_id,
                    trade_date=trade_date,
                    trigger=trigger,
                    source=source,
                    code=code,
                    name=display_name,
                    action="skip_buy",
                    decision="blocked",
                    reason=f"{warning_reason}，自动模拟模式不放行warn级别买入",
                    price=price,
                    amount=amount,
                    candidate_score=score,
                    risk_level=risk.get("final_level"),
                    risk=risk,
                    candidate=candidate,
                ))
                continue

            entry_sector: dict = {}
            if not existing_position:
                entry_sector = await _resolve_candidate_entry_sector(
                    db,
                    candidate,
                    trade_date=trade_date,
                )
                if entry_sector:
                    candidate.update(entry_sector)

            reason = _candidate_buy_reason(candidate, score, stop_loss)
            if limit_up_queue_order:
                reason = (
                    f"{reason}；非一字板回封，按涨停价排队，"
                    "仅在开板或排队后成交量覆盖前方买一队列时确认成交"
                )
            if scale_in:
                reason = f"{reason}；分批建仓：支撑回收再次确认，追加一层"
            if full_conviction and not recovery_buy:
                reason = f"{reason}；高质量回踩确认：仍只执行首层，下一交易日复核后再加仓"
            if recovery_buy:
                if continuous_participation_probe:
                    reason = f"{reason}；账户深回撤盘面观察模式：按评分分层建仓，保留后续确认加仓空间"
                else:
                    recovery_label = "盈利持仓保护下高分信号" if open_count > 0 else "空仓高分信号"
                    reason = f"{reason}；账户回撤恢复模式：{recovery_label}小仓试错"
            if not execute_buy_orders:
                logs.append(await _add_auto_log(
                    db,
                    account_id=account.id,
                    run_id=run_id,
                    trade_date=trade_date,
                    trigger=trigger,
                    source=source,
                    code=code,
                    name=display_name,
                    action="buy",
                    decision="dry_run",
                    reason=reason,
                    price=price,
                    amount=amount,
                    candidate_score=score,
                    risk_level=risk.get("final_level"),
                    risk=risk,
                    candidate=candidate,
                ))
                bought += 1
                if not existing_position:
                    dry_run_new_codes.add(code)
                if sector_key:
                    run_sector_counts[sector_key] = run_sector_counts.get(sector_key, 0) + 1
                continue

            from app.trading.service import SubmitOrderCommand, submit_order

            quote_context = _quote_round_context()
            decision_round_id = str(quote_context.get("round_id") or "")
            defer_to_next_round = bool(
                settings.PAPER_DEFER_AUTO_FILL_TO_NEXT_ROUND
                and execution_mode == "intraday"
                and not limit_up_queue_order
            )
            signal_suffix = decision_round_id or trade_date.strftime("%Y%m%d")
            result = await submit_order(
                db,
                SubmitOrderCommand(
                    code=code,
                    side="buy",
                    price=price,
                    quantity=amount,
                    broker="paper",
                    account_id=account.account_name,   # 五策略并行: 自动买入落账到当前策略账户 (2026-08-31)
                    strategy_id="paper-auto-short",
                    strategy_version=_strategy_version(account.account_name),
                    signal_id=f"auto-{source}-{signal_suffix}-{code}"[:80],
                    source=source,
                    reason=reason,
                    execute=execute_buy_orders,
                    is_drawdown_recovery_probe=recovery_buy,
                    drawdown_recovery_limit_pct=(
                        0
                        if continuous_participation_probe
                        else _drawdown_recovery_limit_pct()
                        if recovery_buy
                        else 0
                    ),
                    entry_sector_code=entry_sector.get("entry_sector_code"),
                    entry_sector_name=entry_sector.get("entry_sector_name"),
                    queue_if_limit_up=limit_up_queue_order,
                    queue_metadata={
                        "candidate": candidate,
                        "confirmed_at": decision_now.isoformat(),
                        "stop_loss_price": stop_loss,
                        "candidate_score": score,
                        "entry_sector_code": entry_sector.get("entry_sector_code"),
                        "entry_sector_name": entry_sector.get("entry_sector_name"),
                        "cancel_time": _highboard_policy(account_name)["queue_cancel_time"],
                        "block_warn": True,
                    } if limit_up_queue_order else None,
                    decision_round_id=decision_round_id,
                    decision_at=decision_now,
                    as_of_at=(
                        quote_context.get("as_of_at")
                        if isinstance(quote_context.get("as_of_at"), datetime)
                        else None
                    ),
                    idempotency_key=(
                        f"{decision_round_id}:{account.account_name}:{source}:buy:{code}"
                        if decision_round_id
                        else ""
                    ),
                    defer_until_next_round=defer_to_next_round,
                    deferred_metadata={
                        "candidate": candidate,
                        "confirmed_at": decision_now.isoformat(),
                        "candidate_score": score,
                        "stop_loss_price": stop_loss,
                        "entry_sector_code": entry_sector.get("entry_sector_code"),
                        "entry_sector_name": entry_sector.get("entry_sector_name"),
                        "block_warn": not _continuous_participation_warn_allowed(
                            risk,
                            continuous_participation_probe,
                        ),
                    } if defer_to_next_round else None,
                ),
            )
            await _refresh_expired_paper_rows(db, account, *logs)
            order = result.get("order") or {}
            fills = result.get("fills") or []
            trade_id = fills[0].get("broker_trade_id") if fills else None
            order_status = str(order.get("status") or "")
            if "confirmation_evidence" in candidate:
                candidate["confirmation_evidence"] = order_confirmation_evidence(candidate, order_status)
            if order_status == "filled":
                action, decision = "buy", "executed"
            elif order_status in {"submitted", "partial"}:
                action = "queue_buy" if limit_up_queue_order else "deferred_buy"
                decision = "wait"
                candidate["pending_order_id"] = order.get("order_id")
                candidate["decision_round_id"] = decision_round_id
            else:
                action, decision = "buy", "blocked"
            logs.append(await _add_auto_log(
                db,
                account_id=account.id,
                run_id=run_id,
                trade_date=trade_date,
                trigger=trigger,
                source=source,
                code=code,
                name=display_name,
                action=action,
                decision=decision,
                reason=order.get("error_message") or reason,
                price=price,
                amount=amount,
                candidate_score=score,
                risk_level=(result.get("risk") or risk).get("final_level"),
                risk=result.get("risk") or risk,
                candidate=candidate,
                executed_trade_id=trade_id,
            ))
            if decision == "executed":
                await _apply_auto_position_risk(
                    db,
                    account.id,
                    code,
                    reason=reason,
                    stop_loss_price=stop_loss,
                )
                held_codes.add(code)
                today_bought_codes.add(code)
                bought += 1
                if sector_key:
                    run_sector_counts[sector_key] = run_sector_counts.get(sector_key, 0) + 1
                account = await _refresh_account(db, account)
            elif order_status in {"submitted", "partial"}:
                queued_codes.add(code)

        await db.commit()
        summary = {
            "executed": sum(1 for item in logs if item.decision == "executed"),
            "blocked": sum(1 for item in logs if item.decision == "blocked"),
            "skipped": sum(1 for item in logs if item.decision == "skipped"),
            "wait": sum(1 for item in logs if item.decision == "wait"),
            "dry_run": sum(1 for item in logs if item.decision == "dry_run"),
        }
        return {
            "run_id": run_id,
            "execute": execute,
            "execution_mode": execution_mode,
            "trade_date": trade_date.isoformat(),
            "summary": summary,
            "logs": [_auto_log_payload(item) for item in logs],
        }


@router.get("/account")
async def paper_account(
    account_name: str = Query(PAPER_ACCOUNT_DEFAULT, description="账户名: default=策略A / promotion=策略B"),
    db: AsyncSession = Depends(get_db),
):
    """模拟盘账户"""
    account = await _get_or_create_account(db, account_name)
    account = await _refresh_account(db, account)
    payload = _account_payload(account)
    payload["accounting"] = await _accounting_display(db, account)
    return {"account": payload}


@router.get("/positions")
async def paper_positions(
    account_name: str = Query(PAPER_ACCOUNT_DEFAULT, description="账户名: default=策略A / promotion=策略B"),
    db: AsyncSession = Depends(get_db),
):
    """当前持仓"""
    account = await _get_or_create_account(db, account_name)
    await _refresh_account(db, account)
    positions = await _open_positions(db, account.id)
    from app.paper.accounting import load_accounting
    accounting = await load_accounting(db, account, as_of=_paper_now().date())
    return {
        "positions": [
            {**_position_payload(item), "accounting": accounting["positions"].get(item.id)}
            for item in positions
        ],
        "accounting_status": accounting["status"],
        "accounting_issues": accounting["issues"],
    }


_MANUAL_ORDER_LOCK = asyncio.Lock()


def _public_order_clock():
    """HTTP时钟只取实际本地时间，不能接受外部轮次/成交元数据回拨。"""
    return datetime.now()


async def _manual_paper_order(req, *, side, account_name, db):
    """兼容旧成功响应，但先留统一委托/风控/成交回报，拒绝不伪装成功。"""
    from app.trading.service import SubmitOrderCommand, submit_order, _existing_order_result

    if account_name in PAPER_CHALLENGER_ACCOUNTS:
        raise HTTPException(403, "隔离候选账户只接受内部前向确认事件委托")
    if account_name not in PAPER_ALL_ACCOUNTS:
        raise HTTPException(400, "未知模拟账户")
    code = req.code.strip()
    if not code:
        raise HTTPException(400, "股票代码不能为空")
    key = ("manual-" + uuid.uuid5(uuid.NAMESPACE_URL,
           json.dumps([account_name, side, req.signal_id], ensure_ascii=False)).hex
           if req.signal_id else "")
    # 独立锁不能复用落账的_TRADE_LOCK，否则submit -> broker -> ledger会死锁。
    async with _MANUAL_ORDER_LOCK:
        existing_order = await _existing_order_result(db, key) if key else None
        if existing_order:
            order = existing_order["order"]
            if (order["code"] != code or order["quantity"] != req.amount
                    or order["price"] != req.price):
                raise HTTPException(409, "幂等信号已用于不同的委托参数")
        account = await db.scalar(select(PaperAccount).where(PaperAccount.account_name == account_name))
        if existing_order is None and account is not None and req.signal_id:
            legacy = await _existing_paper_trade(
                db, account_id=account.id, trade_type=side, signal_id=req.signal_id)
            if legacy is not None:
                if legacy.code != code or legacy.amount != req.amount or legacy.price != req.price:
                    raise HTTPException(409, "幂等信号已用于不同的成交参数")
                # 只返回原凭证，不新建委托、不重贴历史成交时钟。
                return {"status": "ok", "account": _account_payload(account),
                        "trade": _trade_payload(legacy), "idempotent_replay": True}
        result = existing_order or await submit_order(db, SubmitOrderCommand(
            code=code, side=side, price=req.price, quantity=req.amount,
            broker="paper", account_id=account_name, strategy_id="paper-manual",
            signal_id=req.signal_id, source="manual", reason=req.reason,
            decision_at=_public_order_clock(), idempotency_key=key, require_immediate_quote=True,
            stop_loss_price=getattr(req, "stop_loss_price", None),
            entry_sector_code=getattr(req, "entry_sector_code", None),
            entry_sector_name=getattr(req, "entry_sector_name", None),
        ))
        order = result["order"]
        if order["status"] != "filled" or not result["fills"]:
            status = int(result["risk"].get("broker_rejection_http_status") or 400)
            raise HTTPException(status, order.get("error_message") or "模拟委托未成交")
        # TradeFill明确关联的原账本凭证；不按signal模糊挑取另一笔。
        trade_id = result["fills"][0].get("broker_trade_id")
        trade = await db.get(PaperTradeLog, int(trade_id)) if trade_id and str(trade_id).isdigit() else None
        account = await db.scalar(select(PaperAccount).where(PaperAccount.account_name == account_name))
        if trade is None or account is None or trade.account_id != account.id:
            raise HTTPException(409, "委托回报与模拟账本未完整关联，禁止重新成交")
        trade_payload = _trade_payload(trade)
        if side == "sell":
            from app.paper.entry_fee_allocation import load_sale_fee_evidence
            trade_payload["entry_fee_allocation"] = await load_sale_fee_evidence(db, trade)
        return {"status": "ok", "account": _account_payload(account),
                "trade": trade_payload, "order": order, "risk": result["risk"],
                "fills": result["fills"], "idempotent_replay": bool(result.get("idempotent_replay"))}


@router.post("/buy")
async def paper_buy(
    req: SimBuyRequest,
    account_name: str = Query(PAPER_ACCOUNT_DEFAULT, description="账户名"),
    db: AsyncSession = Depends(get_db),
):
    return await _manual_paper_order(req, side="buy", account_name=account_name, db=db)


@router.post("/sell")
async def paper_sell(
    req: SimSellRequest,
    account_name: str = Query(PAPER_ACCOUNT_DEFAULT, description="账户名"),
    db: AsyncSession = Depends(get_db),
):
    return await _manual_paper_order(req, side="sell", account_name=account_name, db=db)


async def _book_paper_buy(
    req: SimBuyRequest,
    *,
    account_name: str,
    db: AsyncSession,
):
    """私有成交落账；不是HTTP路由，元数据上下文不能授予成交权限。"""
    from app.trading.paper_authorization import (
        authorize_ledger_request, validate_ledger_clock, paper_ledger_section,
    )
    authorize_ledger_request(db, req, side="buy", account_name=account_name)
    if (
        account_name in PAPER_CHALLENGER_ACCOUNTS
        and not _CHALLENGER_INTERNAL_ORDER_CONTEXT.get()
    ):
        raise HTTPException(status_code=403, detail="隔离候选账户只接受内部前向确认事件委托")
    async with paper_ledger_section(db):
        validate_ledger_clock(db, phase="lock_acquired")
        account = await _get_or_create_account(db, account_name)
        await db.refresh(account)
        account = await _refresh_account(db, account)
        code = req.code.strip()
        trade_now = _paper_now()
        fill_context = _PAPER_FILL_CONTEXT.get()
        if not code:
            raise HTTPException(status_code=400, detail="股票代码不能为空")
        existing_trade = await _existing_paper_trade(
            db,
            account_id=account.id,
            trade_type="buy",
            signal_id=req.signal_id,
        )
        if existing_trade is not None:
            if existing_trade.code != code or existing_trade.amount != req.amount or existing_trade.price != req.price:
                raise HTTPException(409, "幂等成交请求已用于不同的证券/价量")
            return {
                "status": "ok",
                "account": _account_payload(account),
                "trade": _trade_payload(existing_trade),
                "idempotent_replay": True,
            }
        execution_strategy_version = str(
            _AUTO_STRATEGY_VERSION_CONTEXT.get()
            or _strategy_version(account.account_name)
        )

        amount_value = req.price * req.amount
        commission = _commission(amount_value)
        total_cost = round(amount_value + commission, 2)
        if (account.current_capital or 0) < total_cost:
            raise HTTPException(status_code=400, detail="可用资金不足")

        name, spot_price = await _stock_info(db, code)
        current_price = spot_price or req.price
        reason_text = str(req.reason or "").strip() or str(req.signal_id or "").strip()
        # === 2026-08-31 防御: ST/退市/停牌的股票一律拒绝 (覆盖手动买入路径) ===
        # tag 缺失不直接拒, 但 blacklist 命中必须拒 (2026-08-31 修复 600002 类静默通过)
        tag = (await db.execute(select(StockTag).where(StockTag.code == code))).scalar_one_or_none()
        blocked_reason = (await db.execute(
            select(StockBlacklist).where(
                StockBlacklist.code == code,
                StockBlacklist.reason.in_(["st", "delisting", "suspended"]),
                _active_blacklist_clause(trade_now.date()),
            )
        )).scalar_one_or_none()
        if blocked_reason:
            raise HTTPException(status_code=400, detail=f"{code} 在黑名单({blocked_reason.reason})中, 不允许买入")
        if tag:
            if tag.is_st:
                raise HTTPException(status_code=400, detail=f"{code} 为ST股, 不允许买入")
            if tag.is_suspended:
                raise HTTPException(status_code=400, detail=f"{code} 停牌中, 无法交易")
            if tag.is_delisting:
                raise HTTPException(status_code=400, detail=f"{code} 为退市股, 不允许买入")
        position = (
            await db.execute(
                select(PaperPosition)
                .where(PaperPosition.account_id == account.id, PaperPosition.code == code, PaperPosition.is_closed.is_(False))
                .limit(1)
            )
        ).scalar_one_or_none()
        # All preparation awaits are complete; validate before touching inventory.
        trade_now = validate_ledger_clock(db, phase="before_mutation") or trade_now
        if position:
            position_strategy_version = str(
                getattr(position, "strategy_version", "") or ""
            )
            if position_strategy_version != execution_strategy_version:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "禁止跨策略版本加仓："
                        f"持仓版本={position_strategy_version or 'legacy_unversioned'}，"
                        f"当前版本={execution_strategy_version}"
                    ),
                )
            old_value = position.buy_price * position.buy_amount
            new_value = req.price * req.amount
            position.buy_price = round((old_value + new_value) / (position.buy_amount + req.amount), 4)
            position.buy_amount += req.amount
            position.buy_reason = reason_text or position.buy_reason
            position.current_price = current_price
            # 加仓/T回补不得重置整笔持仓起点，否则会延后交易日到期退出。
            next_stop = req.stop_loss_price or _default_stop_loss_price(position.buy_price)
            if next_stop and next_stop < position.buy_price:
                position.stop_loss_price = round(next_stop, 2)
        else:
            stop_loss_price = req.stop_loss_price or _default_stop_loss_price(req.price)
            position = PaperPosition(
                account_id=account.id,
                code=code,
                name=name,
                buy_price=req.price,
                buy_amount=req.amount,
                buy_time=trade_now,
                buy_reason=reason_text,
                strategy_version=execution_strategy_version,
                entry_sector_code=(str(req.entry_sector_code or "").strip() or None),
                entry_sector_name=(str(req.entry_sector_name or "").strip() or None),
                current_price=current_price,
                profit_loss=round((current_price - req.price) * req.amount, 2),
                profit_pct=round((current_price / req.price - 1) * 100, 2),
                hold_days=0,
                stop_loss_price=round(stop_loss_price, 2) if stop_loss_price else None,
                is_closed=False,
            )
            db.add(position)

        trade = PaperTradeLog(
            account_id=account.id,
            code=code,
            trade_type="buy",
            price=req.price,
            amount=req.amount,
            trade_time=trade_now,
            commission=commission,
            tax=0,
            signal_id=req.signal_id,
            reason=reason_text,
            strategy_version=execution_strategy_version,
            decision_round_id=str(fill_context.get("decision_round_id") or "") or None,
            fill_round_id=str(fill_context.get("fill_round_id") or "") or None,
            forced_probe=bool(fill_context.get("forced_probe", False)),
            excluded_from_performance=bool(
                fill_context.get("excluded_from_performance", False)
            ),
        )
        db.add(trade)
        await db.flush()
        account = await _refresh_account(db, account)
        return {"status": "ok", "account": _account_payload(account), "trade": _trade_payload(trade)}


async def _book_paper_sell(
    req: SimSellRequest,
    *,
    account_name: str,
    db: AsyncSession,
):
    """私有成交落账，仍执行原T+1/持仓版本和账户账务规则。"""
    from app.trading.paper_authorization import (
        authorize_ledger_request, validate_ledger_clock, paper_ledger_section,
    )
    authorize_ledger_request(db, req, side="sell", account_name=account_name)
    if (
        account_name in PAPER_CHALLENGER_ACCOUNTS
        and not _CHALLENGER_INTERNAL_ORDER_CONTEXT.get()
    ):
        raise HTTPException(status_code=403, detail="隔离候选账户只接受内部前向确认事件委托")
    async with paper_ledger_section(db):
        validate_ledger_clock(db, phase="lock_acquired")
        account = await _get_or_create_account(db, account_name)
        await db.refresh(account)
        account = await _refresh_account(db, account)
        code = req.code.strip()
        trade_now = _paper_now()
        fill_context = _PAPER_FILL_CONTEXT.get()
        if not code:
            raise HTTPException(status_code=400, detail="股票代码不能为空")
        existing_trade = await _existing_paper_trade(
            db,
            account_id=account.id,
            trade_type="sell",
            signal_id=req.signal_id,
        )
        if existing_trade is not None:
            if existing_trade.code != code or existing_trade.amount != req.amount or existing_trade.price != req.price:
                raise HTTPException(409, "幂等成交请求已用于不同的证券/价量")
            return {
                "status": "ok",
                "account": _account_payload(account),
                "trade": _trade_payload(existing_trade),
                "idempotent_replay": True,
            }
        position = (
            await db.execute(
                select(PaperPosition)
                .where(PaperPosition.account_id == account.id, PaperPosition.code == code, PaperPosition.is_closed.is_(False))
                .limit(1)
            )
        ).scalar_one_or_none()
        if not position:
            raise HTTPException(status_code=400, detail="当前没有该股票持仓")
        if req.amount > position.buy_amount:
            raise HTTPException(status_code=400, detail="卖出数量超过持仓")
        available_amount = await _available_sell_amount(db, position, trade_now.date())
        if req.amount > available_amount:
            raise HTTPException(status_code=400, detail=f"A股T+1规则：当前可卖隔夜仓{available_amount}股，今日买入部分不能卖出")

        amount_value = req.price * req.amount
        commission = _commission(amount_value)
        stamp_tax = _stamp_tax(amount_value)
        from app.paper.entry_fee_allocation import load_entry_fee_plan, new_sale_accounting
        fee_plan = await load_entry_fee_plan(db, position, sell_quantity=req.amount, at=trade_now)
        if fee_plan["status"] != "known":
            raise HTTPException(409, "卖出买费分摊依据不完整，禁止以零费用伪造成交：" + fee_plan["reason"])
        allocated_buy_commission = fee_plan["allocated_entry_fee"]
        realized_pnl = round(
            (req.price - position.buy_price) * req.amount
            - allocated_buy_commission
            - commission
            - stamp_tax,
            2,
        )

        # Fee/T+1 queries can wait too; dispatch time alone cannot authorize a sale.
        trade_now = validate_ledger_clock(db, phase="before_mutation") or trade_now
        position.buy_amount -= req.amount
        position.current_price = req.price
        if position.buy_amount <= 0:
            position.buy_amount = 0
            position.is_closed = True
        else:
            position.profit_loss = round((req.price - position.buy_price) * position.buy_amount, 2)
            position.profit_pct = round((req.price / position.buy_price - 1) * 100, 2) if position.buy_price else 0

        trade = PaperTradeLog(
            account_id=account.id,
            code=code,
            trade_type="sell",
            price=req.price,
            amount=req.amount,
            trade_time=trade_now,
            commission=commission,
            tax=stamp_tax,
            signal_id=req.signal_id,
            reason=req.reason,
            realized_pnl=realized_pnl,
            strategy_version=(
                str(getattr(position, "strategy_version", "") or "")
                or "legacy_unversioned"
            ),
            decision_round_id=str(fill_context.get("decision_round_id") or "") or None,
            fill_round_id=str(fill_context.get("fill_round_id") or "") or None,
            forced_probe=bool(fill_context.get("forced_probe", False)),
            excluded_from_performance=bool(
                fill_context.get("excluded_from_performance", False)
            ),
        )
        db.add(trade)
        await db.flush()
        fee_evidence = new_sale_accounting(trade, fee_plan)
        db.add(fee_evidence)
        account = await _refresh_account(db, account)
        payload = _trade_payload(trade)
        payload["entry_fee_allocation"] = json.loads(fee_evidence.payload_json)
        return {"status": "ok", "account": _account_payload(account), "trade": payload}


@router.get("/nav")
async def paper_nav(
    account_name: str = Query(PAPER_ACCOUNT_DEFAULT, description="账户名: default=策略A / promotion=策略B"),
    db: AsyncSession = Depends(get_db),
):
    """净值曲线"""
    account = await _get_or_create_account(db, account_name)
    await _refresh_account(db, account)
    result = await db.execute(
        select(PaperNav)
        .where(PaperNav.account_id == account.id)
        .order_by(PaperNav.trade_date)
    )
    return {
        "nav": [
            {
                "date": item.trade_date.isoformat(),
                "trade_date": item.trade_date.isoformat(),
                "nav": item.nav,
                "daily_return": item.daily_return,
            }
            for item in await _filter_reporting_nav(db, list(result.scalars().all()))
        ]
    }


@router.get("/trades")
async def paper_trades(
    limit: int = Query(100, ge=1, le=500),
    account_name: str = Query(PAPER_ACCOUNT_DEFAULT, description="账户名: default=策略A / promotion=策略B"),
    db: AsyncSession = Depends(get_db),
):
    """交易记录：补齐股票中文名、盈亏金额/比例和可读中文信号。"""
    account = await _get_or_create_account(db, account_name)
    history = (
        await db.execute(
            select(PaperTradeLog)
            .where(PaperTradeLog.account_id == account.id)
            .order_by(PaperTradeLog.trade_time, PaperTradeLog.id)
        )
    ).scalars().all()
    if not history:
        return {"trades": []}

    from app.paper.accounting import load_accounting
    accounting = await load_accounting(db, account, as_of=_paper_now().date())
    visible_trades = list(reversed(history[-limit:]))
    visible_ids = [int(item.id) for item in visible_trades if item.id is not None]
    visible_codes = sorted({str(item.code) for item in visible_trades if item.code})

    auto_log_by_trade_id: dict[int, PaperAutoTradeLog] = {}
    if visible_ids:
        linked_logs = (
            await db.execute(
                select(PaperAutoTradeLog)
                .where(
                    PaperAutoTradeLog.executed_trade_id.in_(visible_ids),
                    PaperAutoTradeLog.decision == "executed",
                )
                .order_by(desc(PaperAutoTradeLog.id))
            )
        ).scalars().all()
        for log in linked_logs:
            if log.executed_trade_id is not None:
                auto_log_by_trade_id.setdefault(int(log.executed_trade_id), log)

    positions: list[PaperPosition] = []
    spots: list[StockSpot] = []
    tags: list[StockTag] = []
    if visible_codes:
        positions = (
            await db.execute(
                select(PaperPosition)
                .where(
                    PaperPosition.account_id == account.id,
                    PaperPosition.code.in_(visible_codes),
                )
                .order_by(desc(PaperPosition.id))
            )
        ).scalars().all()
        spots = (
            await db.execute(select(StockSpot).where(StockSpot.code.in_(visible_codes)))
        ).scalars().all()
        tags = (
            await db.execute(select(StockTag).where(StockTag.code.in_(visible_codes)))
        ).scalars().all()

    name_by_code: dict[str, str] = {}
    open_position_by_code: dict[str, PaperPosition] = {}
    for position in positions:
        code = str(position.code or "")
        name = str(position.name or "").strip()
        if code and name:
            name_by_code.setdefault(code, name)
        if code and not position.is_closed:
            open_position_by_code.setdefault(code, position)
    for tag in tags:
        if tag.code and tag.name:
            name_by_code.setdefault(str(tag.code), str(tag.name))
    spot_by_code = {str(spot.code): spot for spot in spots if spot.code}
    for code, spot in spot_by_code.items():
        if spot.name:
            name_by_code[code] = str(spot.name)

    floating_pnl_by_code: dict[str, float] = {}
    floating_pct_by_code: dict[str, float] = {}
    for code, position in open_position_by_code.items():
        spot = spot_by_code.get(code)
        current_price = (
            float(spot.price)
            if spot is not None and spot.price is not None and float(spot.price) > 0
            else float(position.current_price or 0)
        )
        buy_price = float(position.buy_price or 0)
        buy_amount = int(position.buy_amount or 0)
        if current_price > 0 and buy_price > 0:
            floating_pnl_by_code[code] = round(
                (current_price - buy_price) * buy_amount,
                2,
            )
            floating_pct_by_code[code] = round((current_price / buy_price - 1) * 100, 2)
        else:
            if position.profit_loss is not None:
                floating_pnl_by_code[code] = round(float(position.profit_loss), 2)
            if position.profit_pct is not None:
                floating_pct_by_code[code] = round(float(position.profit_pct), 2)

    realized_pct_by_id, open_buy_ids = _paper_trade_pnl_context(history)
    payloads: list[dict] = []
    for trade in visible_trades:
        trade_id = int(trade.id) if trade.id is not None else 0
        code = str(trade.code or "")
        auto_log = auto_log_by_trade_id.get(trade_id)
        display_name = (
            str(auto_log.name or "").strip()
            if auto_log is not None
            else ""
        ) or name_by_code.get(code) or None

        floating_pnl = None
        pnl_pct = None
        pnl_kind = None
        pnl_type_zh = None
        if trade.trade_type == "sell" and trade_id in realized_pct_by_id:
            pnl_pct = realized_pct_by_id[trade_id]
            pnl_kind = "realized"
            pnl_type_zh = "已实现盈亏"
        elif (
            trade.trade_type == "buy"
            and trade_id in open_buy_ids
            and code in floating_pct_by_code
        ):
            floating_pnl = floating_pnl_by_code.get(code)
            pnl_pct = floating_pct_by_code[code]
            pnl_kind = "floating"
            pnl_type_zh = "浮动盈亏"

        payload = _trade_payload(trade)
        payload["accounting"] = accounting["trades"].get(trade_id)
        payload["accounting_status"] = accounting["status"]
        if pnl_kind == "floating":
            payload["pnl"] = floating_pnl
        reason_zh = _paper_trade_reason_zh(trade, auto_log)
        payload.update({
            "name": display_name,
            "floating_pnl": floating_pnl,
            "pnl_pct": pnl_pct,
            "realized_pnl_pct": pnl_pct if pnl_kind == "realized" else None,
            "pnl_kind": pnl_kind,
            "pnl_type_zh": pnl_type_zh,
            "reason_zh": reason_zh,
            "display_reason": reason_zh,
            "raw_reason": trade.reason,
        })
        payloads.append(payload)
    return {"trades": payloads}


@router.get("/challengers/comparison")
async def paper_challenger_comparison(
    horizon_days: int = Query(3, description="前向结算周期: 1/3/5个交易日"),
    recent_limit: int = Query(50, ge=1, le=200),
    account_name: Optional[str] = Query(
        None,
        description="可选的 A-F 基准账户名；传入后仅返回该策略的隔离候选对比",
    ),
    db: AsyncSession = Depends(get_db),
):
    """返回真实模拟账户与前向证据；不会生成演示指标或发送委托。"""
    if horizon_days not in {1, 3, 5}:
        raise HTTPException(status_code=400, detail="horizon_days 仅支持 1/3/5")
    if account_name is not None and account_name not in PAPER_ALL_ACCOUNTS:
        raise HTTPException(status_code=400, detail="account_name 仅支持 A-F 六个基准策略账户")
    from app.paper.strategy_iteration_challenger import (
        build_strategy_iteration_challenger_comparison,
    )

    report = await build_strategy_iteration_challenger_comparison(
        db,
        horizon_days=horizon_days,
        recent_limit=recent_limit,
        account_name=account_name,
    )
    # Never pool principal, cash or PnL across champion/challenger accounts.
    accounting_by_id = {}
    for pair in report.get("pairs", []):
        for side in ("champion", "challenger"):
            payload = pair.get(side) or {}
            account_id = payload.get("id")
            if account_id is None:
                continue
            if account_id not in accounting_by_id:
                with db.no_autoflush:
                    account = await db.scalar(select(PaperAccount).where(PaperAccount.id == account_id))
                if account is not None:
                    accounting_by_id[account_id] = await _accounting_display(db, account)
            accounting = accounting_by_id.get(account_id)
            if accounting is not None:
                payload["accounting"] = accounting
                for position in payload.get("positions", []):
                    position["accounting"] = accounting["positions"].get(position.get("id"))
    return report


@router.get("/experiment/report")
async def paper_experiment_report(
    account_name: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """持续实验只读报告；绝不把观察、旧版本或未平仓当成交胜率。"""
    if account_name is not None and account_name not in (*PAPER_ALL_ACCOUNTS, *PAPER_CHALLENGER_ACCOUNTS):
        raise HTTPException(status_code=400, detail="未知模拟账户")
    from app.paper.experiment_report import build_experiment_report
    return await build_experiment_report(db, account_name=account_name)


@router.get("/audit/daily-outcomes")
async def paper_daily_outcomes(
    target_date: Optional[date] = Query(None, description="交易日，默认最近已固化交易日"),
    account_name: Optional[str] = Query(None, description="可选账户名"),
    db: AsyncSession = Depends(get_db),
):
    """运行SLA与控制样本审计；控制/强制样本明确排除在绩效外。"""
    if account_name is not None and account_name not in {
        *PAPER_ALL_ACCOUNTS,
        *PAPER_CHALLENGER_ACCOUNTS,
    }:
        raise HTTPException(status_code=400, detail="未知模拟账户")
    if target_date is None:
        target_date = await db.scalar(
            select(func.max(PaperDailyOutcome.trade_date))
        )
    if target_date is None:
        return {"trade_date": None, "outcomes": [], "control_samples": []}
    all_accounts = list(
        (
            await db.scalars(
                select(PaperAccount).where(
                    PaperAccount.account_name.in_(
                        (*PAPER_ALL_ACCOUNTS, *PAPER_CHALLENGER_ACCOUNTS)
                    )
                )
            )
        ).all()
    )
    # 映射始终包含基准与控制账户两端；筛选只影响可见结果，不能把关联名抹成None。
    account_by_id = {item.id: item.account_name for item in all_accounts}
    account_ids = [
        item.id
        for item in all_accounts
        if account_name is None or item.account_name == account_name
    ]
    outcomes = (
        list(
            (
                await db.scalars(
                    select(PaperDailyOutcome)
                    .where(
                        PaperDailyOutcome.trade_date == target_date,
                        PaperDailyOutcome.account_id.in_(account_ids),
                    )
                    .order_by(PaperDailyOutcome.account_id)
                )
            ).all()
        )
        if account_ids
        else []
    )
    samples = list(
        (
            await db.scalars(
                select(PaperControlSample)
                .where(PaperControlSample.trade_date == target_date)
                .order_by(PaperControlSample.account_id)
            )
        ).all()
    )
    visible_account_ids = set(account_ids)
    return {
        "trade_date": target_date.isoformat(),
        "outcomes": [
            {
                "account_name": account_by_id.get(item.account_id),
                "strategy_version": item.strategy_version,
                "terminal_status": item.terminal_status,
                "reason_code": item.reason_code,
                "reason": item.reason,
                "scan_count": item.scan_count,
                "decision_count": item.decision_count,
                "submitted_order_count": item.submitted_order_count,
                "fill_count": item.fill_count,
                "blocked_count": item.blocked_count,
                "first_round_id": item.first_round_id,
                "last_round_id": item.last_round_id,
                "control_sample_id": item.control_sample_id,
                "is_terminal": item.is_terminal,
                "details": _json_loads_dict(item.details_json),
                "finalized_at": (
                    item.finalized_at.isoformat(sep=" ")
                    if item.finalized_at
                    else None
                ),
            }
            for item in outcomes
        ],
        "control_samples": [
            {
                "id": item.id,
                "account_name": account_by_id.get(item.account_id),
                "challenger_account_name": account_by_id.get(
                    item.challenger_account_id
                ),
                "strategy_version": item.strategy_version,
                "quote_round_id": item.quote_round_id,
                "observed_at": item.observed_at.isoformat(sep=" "),
                "source": item.source,
                "code": item.code,
                "name": item.name,
                "price": item.price,
                "candidate_score": item.candidate_score,
                "decision": item.decision,
                "reason_code": item.reason_code,
                "reason": item.reason,
                "next_round_id": item.next_round_id,
                "next_round_price": item.next_round_price,
                "next_round_fillable_amount": item.next_round_fillable_amount,
                "close_price": item.close_price,
                "return_pct": item.return_pct,
                "forced_probe": item.forced_probe,
                "excluded_from_performance": item.excluded_from_performance,
                "finalized_at": (
                    item.finalized_at.isoformat(sep=" ")
                    if item.finalized_at
                    else None
                ),
            }
            for item in samples
            if (
                not account_name
                or item.account_id in visible_account_ids
                or item.challenger_account_id in visible_account_ids
            )
        ],
    }


@router.get("/auto/status")
async def paper_auto_status(
    account_name: str = Query(PAPER_ACCOUNT_DEFAULT, description="账户名: default=策略A / promotion=策略B"),
    db: AsyncSession = Depends(get_db),
):
    """自动模拟交易状态"""
    today = date.today()
    order_window_ok, order_window_reason = await _paper_order_window_status()
    account = await _get_or_create_account(db, account_name)
    open_positions = await _open_positions(db, account.id)
    intraday_buy_start = _strategy_intraday_buy_start(account_name)
    scope_filter = _auto_log_account_scope_filter(account_name, account.id)
    latest = (
        await db.execute(
            select(PaperAutoTradeLog)
            .where(scope_filter, PaperAutoTradeLog.action.notin_(("buy_signal", "signal_push")))
            .order_by(desc(PaperAutoTradeLog.created_at))
            .limit(1)
        )
    ).scalar_one_or_none()
    today_rows = (
        await db.execute(
            select(PaperAutoTradeLog)
            .where(
                PaperAutoTradeLog.trade_date == today,
                scope_filter,
                PaperAutoTradeLog.action.notin_(("buy_signal", "signal_push")),
            )
            .order_by(desc(PaperAutoTradeLog.created_at))
        )
    ).scalars().all()
    latest_outcome = await db.scalar(
        select(PaperDailyOutcome)
        .where(PaperDailyOutcome.account_id == account.id)
        .order_by(desc(PaperDailyOutcome.trade_date), desc(PaperDailyOutcome.id))
        .limit(1)
    )
    from app.paper.experiment_report import runtime_scan_evidence
    from app.paper.account_policy import account_parameter_snapshot
    current_version = _strategy_version(account_name)
    current_logs = [row for row in today_rows if row.strategy_version == current_version]
    status_payload = {
        "runtime": runtime_scan_evidence(current_logs, as_of=_paper_now()),
        "effective_parameters": account_parameter_snapshot(account_name) if account_name in (*PAPER_ALL_ACCOUNTS, *PAPER_CHALLENGER_ACCOUNTS) else None,
        "enabled": settings.PAPER_AUTO_TRADE_ENABLED,
        "auto_order_enabled": _strategy_auto_order_enabled(account_name),
        "experiment": experiment_status(account_name, at=_paper_now()),
        "account_scope": {
            "account_id": account.id,
            "account_name": account.account_name,
        },
        "current_positions": {
            "count": len(open_positions),
            "codes": [position.code for position in open_positions],
        },
        "intraday_enabled": settings.PAPER_INTRADAY_AUTO_TRADE_ENABLED,
        "intraday_interval_sec": settings.PAPER_INTRADAY_AUTO_INTERVAL_SEC,
        "execution_model": {
            "decision_snapshot": "immutable_quote_round",
            "fill_timing": (
                "next_healthy_quote_round"
                if settings.PAPER_DEFER_AUTO_FILL_TO_NEXT_ROUND
                else "same_request"
            ),
            "depth": "visible_l1_l5_partial_fill",
            "minimum_commission": settings.PAPER_MIN_COMMISSION,
            "stamp_tax_rate": settings.PAPER_STAMP_TAX_RATE,
            "forced_probe_enabled": settings.PAPER_FORCED_PROBE_ENABLED,
        },
        "daily_outcome": (
            {
                "trade_date": latest_outcome.trade_date.isoformat(),
                "terminal_status": latest_outcome.terminal_status,
                "reason_code": latest_outcome.reason_code,
                "reason": latest_outcome.reason,
                "is_terminal": latest_outcome.is_terminal,
                "fill_count": latest_outcome.fill_count,
                "blocked_count": latest_outcome.blocked_count,
                "finalized_at": (
                    latest_outcome.finalized_at.isoformat(sep=" ")
                    if latest_outcome.finalized_at
                    else None
                ),
            }
            if latest_outcome is not None
            else None
        ),
        "intraday_buy_window": {
            "start": intraday_buy_start,
            "end": settings.PAPER_INTRADAY_BUY_END,
        },
        "max_positions": settings.PAPER_AUTO_MAX_POSITIONS,
        "position_pct": settings.PAPER_AUTO_POSITION_PCT,
        "position_policy": {
            "style": "short_swing_layers",
            "trial_pct": settings.PAPER_AUTO_STAGED_ENTRY_TRIAL_PCT,
            "normal_pct": settings.PAPER_AUTO_STAGED_ENTRY_NORMAL_PCT,
            "strong_pct": settings.PAPER_AUTO_STAGED_ENTRY_STRONG_PCT,
            "core_pct": settings.PAPER_AUTO_STAGED_ENTRY_MAX_POSITION_PCT,
            "scale_in_enabled": settings.PAPER_AUTO_SCALE_IN_ENABLED,
            "scale_in_min_score": settings.PAPER_AUTO_SCALE_IN_MIN_SCORE,
            "scale_in_cost_range_pct": [
                settings.PAPER_AUTO_SCALE_IN_MIN_COST_RETURN_PCT,
                settings.PAPER_AUTO_SCALE_IN_MAX_COST_RETURN_PCT,
            ],
            "drawdown_half_line": settings.PAPER_AUTO_DRAWDOWN_HALF_BUY_PCT,
            "drawdown_pause_line": settings.PAPER_AUTO_DRAWDOWN_PAUSE_BUY_PCT,
            "drawdown_recovery_enabled": settings.PAPER_AUTO_DRAWDOWN_RECOVERY_ENABLED,
            "drawdown_recovery_hard_pause_line": settings.PAPER_AUTO_DRAWDOWN_RECOVERY_HARD_PAUSE_PCT,
            "drawdown_recovery_min_score": settings.PAPER_AUTO_DRAWDOWN_RECOVERY_MIN_SCORE,
            "drawdown_permanent_lock_enabled": settings.PAPER_AUTO_DRAWDOWN_PERMANENT_LOCK_ENABLED,
            "drawdown_hard_recovery_max_pct": settings.PAPER_AUTO_DRAWDOWN_HARD_RECOVERY_MAX_PCT,
            "drawdown_hard_recovery_min_score": settings.PAPER_AUTO_DRAWDOWN_HARD_RECOVERY_MIN_SCORE,
            "drawdown_recovery_pct": settings.PAPER_AUTO_DRAWDOWN_RECOVERY_POSITION_PCT,
            "drawdown_recovery_max_buys": settings.PAPER_AUTO_DRAWDOWN_RECOVERY_MAX_BUYS,
            "drawdown_recovery_max_amount": settings.PAPER_AUTO_DRAWDOWN_RECOVERY_MAX_AMOUNT,
            "continuous_participation_enabled": settings.PAPER_AUTO_DRAWDOWN_CONTINUOUS_PARTICIPATION_ENABLED,
            "continuous_participation_min_score": settings.PAPER_AUTO_DRAWDOWN_CONTINUOUS_PARTICIPATION_MIN_SCORE,
            "continuous_participation_max_buys": settings.PAPER_AUTO_DRAWDOWN_CONTINUOUS_PARTICIPATION_MAX_BUYS,
            "continuous_participation_max_amount": settings.PAPER_AUTO_DRAWDOWN_CONTINUOUS_PARTICIPATION_MAX_AMOUNT,
            "full_conviction_enabled": settings.PAPER_AUTO_FULL_CONVICTION_ENABLED,
        },
        "signal_policy": {
            "buy_source": "高胜率预案 + 异动买点 + 盘面滚动观察",
            "sector_filter": f"异动买点要求板块强度≥{settings.PAPER_AUTO_ANOMALY_MIN_SECTOR_STRENGTH:.0f}，过滤冷门、泛概念、过热追涨和尾盘信号",
            "risk_filter": "warn默认拦截；深回撤只允许已确认性价比买点分层建仓，其他风控不豁免",
            "value_entry_guard": (
                f"只做回踩/支撑回收；涨幅>{settings.PAPER_AUTO_VALUE_ENTRY_MAX_CHANGE_PCT:.1f}%、"
                f"距日高≤{settings.PAPER_AUTO_VALUE_ENTRY_MAX_HIGH_GAP_PCT:.1f}%或"
                f"高于VWAP>{settings.PAPER_AUTO_VALUE_ENTRY_MAX_VWAP_PREMIUM_PCT:.1f}%时不追价；"
                "水下/低开急拉须等待5分钟首次停顿确认"
            ),
            "empty_position_policy": (
                "正常交易日持续滚动扫描；不强迫成交。仅A档强趋势同时满足VWAP、位置和至少"
                f"{settings.PAPER_AUTO_DAILY_PARTICIPATION_MIN_LIQUIDITY_CONFIRMATIONS}项盘口/资金确认时建立首层，"
                "后续只在下一交易日支撑再次回收时加仓"
            ),
            "sell_guard": "持仓止损价/硬止损优先，开盘噪声窗口内暂缓小止损和分时弱化卖点",
        },
        "order_window": {
            "can_order": order_window_ok,
            "reason": order_window_reason,
            "buy_window_open": order_window_ok and _is_intraday_buy_window(
                start_value=intraday_buy_start
            ),
        },
        "min_score": settings.PAPER_AUTO_MIN_SCORE,
        "short_trade_rules": {
            "take_profit_pct": settings.PAPER_AUTO_TAKE_PROFIT_PCT,
            "hard_stop_loss_pct": settings.PAPER_AUTO_STOP_LOSS_PCT,
            "small_stop_loss_pct": settings.PAPER_AUTO_SMALL_STOP_LOSS_PCT,
            "pullback_from_high_pct": settings.PAPER_AUTO_PULLBACK_FROM_HIGH_PCT,
            "max_hold_days": settings.PAPER_AUTO_MAX_HOLD_DAYS,
        },
        "last_run": _auto_log_payload(latest) if latest else None,
        "today": {
            "date": today.isoformat(),
            "total": len(today_rows),
            "executed": sum(1 for item in today_rows if item.decision == "executed"),
            "buy_executed": sum(
                1 for item in today_rows
                if item.action == "buy" and item.decision == "executed"
            ),
            "new_buy_executed": sum(
                1 for item in today_rows
                if item.action == "buy"
                and item.decision == "executed"
                and item.source != "position-t"
            ),
            "sell_executed": sum(
                1 for item in today_rows
                if item.action == "sell" and item.decision == "executed"
            ),
            "dry_run_buys": sum(
                1 for item in today_rows
                if item.action == "buy" and item.decision == "dry_run"
            ),
            "dry_run_buy_candidates": len({
                item.code for item in today_rows
                if item.action == "buy"
                and item.decision == "dry_run"
                and item.code
            }),
            "queued_buys": sum(
                1 for item in today_rows
                if item.action == "queue_buy" and item.decision == "wait"
            ),
            "blocked": sum(1 for item in today_rows if item.decision == "blocked"),
            "skipped": sum(1 for item in today_rows if item.decision == "skipped"),
            "wait": sum(1 for item in today_rows if item.decision == "wait"),
            "dry_run": sum(1 for item in today_rows if item.decision == "dry_run"),
        },
    }
    # === 2026-08-31 五策略并行: B/C/D/E 覆盖展示口径为独立参数 ===
    override = _strategy_status_override(account_name)
    if override:
        return {**status_payload, **override}
    return status_payload


def _strategy_status_override(account_name: str) -> dict:
    """各路线真实执行参数，不把次账户误展示为A的参数。"""
    if account_name == PAPER_ACCOUNT_CHALLENGER_A:
        params = _strategy_sell_params_by_name(account_name)
        return {
            "strategy": account_name, "strategy_label": "A2首次回踩确认",
            "max_positions": settings.PAPER_CHALLENGER_A_MAX_POSITIONS,
            "position_pct": settings.PAPER_CHALLENGER_A_POSITION_PCT,
            "position_policy": {**params, "daily_max_buys": settings.PAPER_CHALLENGER_A_MAX_DAILY_BUYS},
            "short_trade_rules": {**params, "hard_stop_loss_pct": params["stop_loss_pct"]},
            "signal_policy": {"buy_source": "momentum_first_retest", "risk_filter": "本策略报价/路径/盘口数据必须完整；情绪仅作分层记录"},
        }
    if account_name in PAPER_CHALLENGER_ACCOUNTS:
        from app.paper.account_policy import account_entry_exit_policy
        policy = account_entry_exit_policy(account_name)
        params = _strategy_sell_params_by_name(account_name)
        labels = {
            PAPER_ACCOUNT_CHALLENGER_B: "B2弱开二板确认",
            PAPER_ACCOUNT_CHALLENGER_C: "C2近期涨停再启动",
            PAPER_ACCOUNT_CHALLENGER_D: "D2竞价修复",
            PAPER_ACCOUNT_CHALLENGER_E: "E2高标强势/回封实验",
            PAPER_ACCOUNT_CHALLENGER_F2: "F2高标断板回收",
        }
        route = next((key for key, value in PAPER_CHALLENGER_ACCOUNT_BY_ROUTE.items()
                      if value == account_name), "tenbagger_midline")
        signal_policy = {
            "buy_source": route,
            "risk_filter": "独立账户参数；T+1/资格/真实报价/现金仓位/撮合约束保留",
            "sell_guard": "持仓优先读取首次建仓订单冻结退出参数；旧证据缺失会单独标记",
        }
        if account_name == PAPER_ACCOUNT_CHALLENGER_E:
            highboard = _highboard_policy(account_name)
            signal_policy.update({
                "candidate_filter": (
                    f"昨日{highboard['min_consecutive']}-{highboard['max_consecutive']}板；"
                    f"封单≥{highboard['min_seal_amount']}亿；炸板≤{highboard['max_break_count']}次；"
                    f"入口涨幅≤{highboard['max_entry_change_pct']}%；VWAP/高点回撤策略过滤保留"
                ),
                "empty_position_policy": (
                    "每日扫描昨日高标；曾开板回封可排队，只有后续真实盘口/量能证据才成交；"
                    f"{highboard['queue_cancel_time']}未成交自动撤单"
                ),
            })
        return {
            "strategy": account_name, "strategy_label": labels[account_name],
            "max_positions": policy["max_positions"], "position_pct": policy["position_pct"],
            "position_policy": {**policy, "daily_max_buys": policy["max_daily_buys"],
                                "max_position_pct": policy["position_pct"]},
            "short_trade_rules": {**params, "hard_stop_loss_pct": params["stop_loss_pct"]},
            "signal_policy": signal_policy,
        }
    if account_name == PAPER_ACCOUNT_PROMOTION:
        return {
            "strategy": "promotion",
            "strategy_label": "晋级预测二板（基准策略）",
            "max_positions": settings.PAPER_PROMOTION_MAX_POSITIONS,
            "position_pct": settings.PAPER_PROMOTION_POSITION_PCT,
            "position_policy": {
                "style": "single_layer_promotion",
                "max_position_pct": settings.PAPER_PROMOTION_POSITION_PCT,
                "daily_max_buys": settings.PAPER_PROMOTION_MAX_DAILY_BUYS,
                "take_profit_pct": settings.PAPER_PROMOTION_TAKE_PROFIT_PCT,
                "stop_loss_pct": settings.PAPER_PROMOTION_STOP_LOSS_PCT,
                "max_hold_days": settings.PAPER_PROMOTION_MAX_HOLD_DAYS,
                "min_probability": settings.PAPER_PROMOTION_MIN_PROBABILITY,
                "trial_pct": settings.PAPER_PROMOTION_POSITION_PCT,
                "normal_pct": settings.PAPER_PROMOTION_POSITION_PCT,
                "strong_pct": settings.PAPER_PROMOTION_POSITION_PCT,
                "core_pct": settings.PAPER_PROMOTION_POSITION_PCT,
            },
            "signal_policy": {
                "buy_source": "晋级预测二板赛道（基准策略）",
                "candidate_filter": (
                    f"信号日次日盘中确认：涨幅 {settings.PAPER_PROMOTION_MIN_INTRADAY_CONFIRM_CHANGE_PCT:.1f}%"
                    f"~{settings.PAPER_PROMOTION_MAX_INTRADAY_CONFIRM_CHANGE_PCT:.1f}%（不低开不追高），"
                    "非一字板，量比≥0.6，过滤ST/停牌/退市"
                ),
                "risk_filter": "单笔硬止损仓位上限生效；回撤开仓闸门 unlimited(观测层放开)",
                "value_entry_guard": (
                    f"晋级预测概率≥阈值且盘中确认才入场，不因噪音离场"
                    f"(独立止盈{settings.PAPER_PROMOTION_TAKE_PROFIT_PCT}%/止损{settings.PAPER_PROMOTION_STOP_LOSS_PCT}%/持仓≤{settings.PAPER_PROMOTION_MAX_HOLD_DAYS}天)"
                ),
                "empty_position_policy": "每日滚动扫描晋级二板正式快照；概率不足或未确认时不强迫成交",
                "sell_guard": (
                    f"策略B独立参数：止盈{settings.PAPER_PROMOTION_TAKE_PROFIT_PCT}%/硬止损{settings.PAPER_PROMOTION_STOP_LOSS_PCT}%/"
                    f"时间止损{settings.PAPER_PROMOTION_MAX_HOLD_DAYS}天，无小止损/次日不强就走噪音卖点"
                ),
            },
            "short_trade_rules": {
                "take_profit_pct": settings.PAPER_PROMOTION_TAKE_PROFIT_PCT,
                "hard_stop_loss_pct": settings.PAPER_PROMOTION_STOP_LOSS_PCT,
                "small_stop_loss_pct": settings.PAPER_PROMOTION_STOP_LOSS_PCT,
                "max_hold_days": settings.PAPER_PROMOTION_MAX_HOLD_DAYS,
            },
        }
    if account_name == PAPER_ACCOUNT_MAINLINE:
        return {
            "strategy": "mainline",
            "strategy_label": "主线扩散首板（基准策略）",
            "max_positions": settings.PAPER_MAINLINE_MAX_POSITIONS,
            "position_pct": settings.PAPER_MAINLINE_POSITION_PCT,
            "position_policy": {
                "style": "single_layer_mainline",
                "max_position_pct": settings.PAPER_MAINLINE_POSITION_PCT,
                "daily_max_buys": settings.PAPER_MAINLINE_MAX_DAILY_BUYS,
                "take_profit_pct": settings.PAPER_MAINLINE_TAKE_PROFIT_PCT,
                "stop_loss_pct": settings.PAPER_MAINLINE_STOP_LOSS_PCT,
                "max_hold_days": settings.PAPER_MAINLINE_MAX_HOLD_DAYS,
                "min_probability": None,
                "probability_policy": "governed_actionable_or_0935_mainline_live_confirm",
                "trial_pct": settings.PAPER_MAINLINE_POSITION_PCT,
                "normal_pct": settings.PAPER_MAINLINE_POSITION_PCT,
                "strong_pct": settings.PAPER_MAINLINE_POSITION_PCT,
                "core_pct": settings.PAPER_MAINLINE_POSITION_PCT,
            },
            "signal_policy": {
                "buy_source": "晋级预测·主线扩散首板（基准策略）",
                "candidate_filter": (
                    f"信号日次日盘中确认：涨幅 {settings.PAPER_MAINLINE_MIN_INTRADAY_CONFIRM_CHANGE_PCT:.1f}%"
                    f"~{settings.PAPER_MAINLINE_MAX_INTRADAY_CONFIRM_CHANGE_PCT:.1f}%（补涨不追高），"
                    "非一字板，量比≥0.6，过滤ST/停牌/退市"
                ),
                "risk_filter": "单笔硬止损仓位上限生效；回撤开仓闸门 unlimited(观测层放开)",
                "value_entry_guard": (
                    f"主线板块内补涨首板：优先消费治理快照已标记可执行候选；"
                    f"否则仅允许当日09:35正式榜/召回榜完成同板块强度≥"
                    f"{settings.PAPER_MAINLINE_LIVE_CONFIRM_MIN_SECTOR_STRENGTH:.0f}、"
                    f"上涨、资金净流入且涨停≥{settings.PAPER_MAINLINE_LIVE_CONFIRM_MIN_SECTOR_LIMIT_UP_COUNT}家后二次确认"
                    f"(独立止盈{settings.PAPER_MAINLINE_TAKE_PROFIT_PCT}%/止损{settings.PAPER_MAINLINE_STOP_LOSS_PCT}%/持仓≤{settings.PAPER_MAINLINE_MAX_HOLD_DAYS}天)"
                ),
                "empty_position_policy": "不回退旧批次；09:35主线榜未形成同板块实时扩散确认时不强迫成交",
                "sell_guard": (
                    f"策略C独立参数：止盈{settings.PAPER_MAINLINE_TAKE_PROFIT_PCT}%/硬止损{settings.PAPER_MAINLINE_STOP_LOSS_PCT}%/"
                    f"时间止损{settings.PAPER_MAINLINE_MAX_HOLD_DAYS}天，无短线噪音卖点"
                ),
            },
            "short_trade_rules": {
                "take_profit_pct": settings.PAPER_MAINLINE_TAKE_PROFIT_PCT,
                "hard_stop_loss_pct": settings.PAPER_MAINLINE_STOP_LOSS_PCT,
                "small_stop_loss_pct": settings.PAPER_MAINLINE_STOP_LOSS_PCT,
                "max_hold_days": settings.PAPER_MAINLINE_MAX_HOLD_DAYS,
            },
        }
    if account_name == PAPER_ACCOUNT_AUCTION:
        return {
            "strategy": "auction",
            "strategy_label": "竞价高开强攻（基准策略）",
            "max_positions": settings.PAPER_AUCTION_MAX_POSITIONS,
            "position_pct": settings.PAPER_AUCTION_POSITION_PCT,
            "position_policy": {
                "style": "single_layer_auction",
                "max_position_pct": settings.PAPER_AUCTION_POSITION_PCT,
                "daily_max_buys": settings.PAPER_AUCTION_MAX_DAILY_BUYS,
                "take_profit_pct": settings.PAPER_AUCTION_TAKE_PROFIT_PCT,
                "stop_loss_pct": settings.PAPER_AUCTION_STOP_LOSS_PCT,
                "max_hold_days": settings.PAPER_AUCTION_MAX_HOLD_DAYS,
                "min_probability": None,
                "probability_policy": "governed_snapshot_actionable",
                "trial_pct": settings.PAPER_AUCTION_POSITION_PCT,
                "normal_pct": settings.PAPER_AUCTION_POSITION_PCT,
                "strong_pct": settings.PAPER_AUCTION_POSITION_PCT,
                "core_pct": settings.PAPER_AUCTION_POSITION_PCT,
            },
            "signal_policy": {
                "buy_source": "晋级预测·竞价高开强攻（基准策略）",
                "candidate_filter": (
                    f"信号日次日盘中确认：涨幅 {settings.PAPER_AUCTION_MIN_INTRADAY_CONFIRM_CHANGE_PCT:.1f}%"
                    f"~{settings.PAPER_AUCTION_MAX_INTRADAY_CONFIRM_CHANGE_PCT:.1f}%（要求高开不追板），"
                    "非一字板，量比≥0.6，过滤ST/停牌/退市"
                ),
                "risk_filter": "单笔硬止损仓位上限生效；回撤开仓闸门 unlimited(观测层放开)",
                "value_entry_guard": "竞价高开+板块确认强攻，仅消费治理快照已标记可执行且盘中确认的候选(独立止盈5%/止损4%/持仓≤2天)",
                "empty_position_policy": "每日滚动扫描竞价高开正式快照；竞价字段不完整或上游未判定可执行时不强迫成交",
                "sell_guard": "策略D独立参数：止盈5%/硬止损4%/时间止损2天，无短线噪音卖点",
            },
            "short_trade_rules": {
                "take_profit_pct": settings.PAPER_AUCTION_TAKE_PROFIT_PCT,
                "hard_stop_loss_pct": settings.PAPER_AUCTION_STOP_LOSS_PCT,
                "small_stop_loss_pct": settings.PAPER_AUCTION_STOP_LOSS_PCT,
                "max_hold_days": settings.PAPER_AUCTION_MAX_HOLD_DAYS,
            },
        }
    if account_name == PAPER_ACCOUNT_TENBAGGER:
        return {
            "strategy": "tenbagger",
            "strategy_label": "连板高标接力（基准策略）",
            "max_positions": settings.PAPER_TENBAGGER_MAX_POSITIONS,
            "position_pct": settings.PAPER_TENBAGGER_POSITION_PCT,
            "position_policy": {
                "style": "high_board_relay",
                "max_position_pct": settings.PAPER_TENBAGGER_POSITION_PCT,
                "daily_max_buys": settings.PAPER_TENBAGGER_MAX_DAILY_BUYS,
                "take_profit_pct": settings.PAPER_HIGHBOARD_TAKE_PROFIT_PCT,
                "stop_loss_pct": settings.PAPER_HIGHBOARD_STOP_LOSS_PCT,
                "max_hold_days": settings.PAPER_HIGHBOARD_MAX_HOLD_DAYS,
                "min_consecutive": settings.PAPER_HIGHBOARD_MIN_CONSECUTIVE,
                "max_consecutive": settings.PAPER_HIGHBOARD_MAX_CONSECUTIVE,
                "min_seal_amount": settings.PAPER_HIGHBOARD_MIN_SEAL_AMOUNT,
                "max_break_count": settings.PAPER_HIGHBOARD_MAX_BREAK_COUNT,
                "trial_pct": settings.PAPER_TENBAGGER_POSITION_PCT,
                "normal_pct": settings.PAPER_TENBAGGER_POSITION_PCT,
                "strong_pct": settings.PAPER_TENBAGGER_POSITION_PCT,
                "core_pct": settings.PAPER_TENBAGGER_POSITION_PCT,
            },
            "signal_policy": {
                "buy_source": "连板高标接力（基准策略）",
                "candidate_filter": (
                    f"连板 {settings.PAPER_HIGHBOARD_MIN_CONSECUTIVE}-{settings.PAPER_HIGHBOARD_MAX_CONSECUTIVE} 板，"
                    f"封板资金≥{settings.PAPER_HIGHBOARD_MIN_SEAL_AMOUNT:.0f}亿，"
                    f"炸板≤{settings.PAPER_HIGHBOARD_MAX_BREAK_COUNT}次，"
                    f"{settings.PAPER_HIGHBOARD_INTRADAY_BUY_START}起开板可成交；"
                    f"入口涨幅≤{settings.PAPER_TENBAGGER_MAX_INTRADAY_CONFIRM_CHANGE_PCT}%，过滤ST/停牌/退市；"
                    f"13:00后沿用高标自身确认，{settings.PAPER_AUTO_LATE_NEW_BUY_CUTOFF}后不再新报单"
                ),
                "risk_filter": "单笔硬止损仓位上限生效；回撤开仓闸门 unlimited(观测层放开)",
                "value_entry_guard": (
                    f"连板高标接力：持有≤{settings.PAPER_HIGHBOARD_MAX_HOLD_DAYS}日"
                    f"(独立止盈{settings.PAPER_HIGHBOARD_TAKE_PROFIT_PCT}%/止损{settings.PAPER_HIGHBOARD_STOP_LOSS_PCT}%)"
                ),
                "empty_position_policy": (
                    "每日滚动扫描昨日高标的低位可成交入口；不满足本策略涨幅上限则等待，"
                    "不为凑交易改成追板。强势/回封入口由独立E2账户验证"
                ),
                "sell_guard": (
                    f"策略E高标接力卖出：止盈{settings.PAPER_HIGHBOARD_TAKE_PROFIT_PCT}%/硬止损{settings.PAPER_HIGHBOARD_STOP_LOSS_PCT}%/"
                    f"跌破MA20/到期{settings.PAPER_HIGHBOARD_MAX_HOLD_DAYS}日平仓"
                ),
            },
            "short_trade_rules": {
                "take_profit_pct": settings.PAPER_HIGHBOARD_TAKE_PROFIT_PCT,
                "hard_stop_loss_pct": settings.PAPER_HIGHBOARD_STOP_LOSS_PCT,
                "small_stop_loss_pct": settings.PAPER_HIGHBOARD_STOP_LOSS_PCT,
                "max_hold_days": settings.PAPER_HIGHBOARD_MAX_HOLD_DAYS,
            },
        }
    if account_name == PAPER_ACCOUNT_REVERSAL:
        return {
            "strategy": "reversal",
            "strategy_label": "断板反包（基准策略）",
            "max_positions": settings.PAPER_REVERSAL_MAX_POSITIONS,
            "position_pct": settings.PAPER_REVERSAL_POSITION_PCT,
            "position_policy": {
                "style": "break_reversal",
                "max_position_pct": settings.PAPER_REVERSAL_POSITION_PCT,
                "daily_max_buys": settings.PAPER_REVERSAL_MAX_DAILY_BUYS,
                "take_profit_pct": settings.PAPER_REVERSAL_TAKE_PROFIT_PCT,
                "stop_loss_pct": settings.PAPER_REVERSAL_STOP_LOSS_PCT,
                "max_hold_days": settings.PAPER_REVERSAL_MAX_HOLD_DAYS,
                "min_consecutive": settings.PAPER_REVERSAL_MIN_CONSECUTIVE,
                "max_gap_days": settings.PAPER_REVERSAL_MAX_GAP_DAYS,
                "min_dip_pct": settings.PAPER_REVERSAL_MIN_DIP_PCT,
                "min_vol_ratio": settings.PAPER_REVERSAL_MIN_VOL_RATIO,
                "trial_pct": settings.PAPER_REVERSAL_POSITION_PCT,
                "normal_pct": settings.PAPER_REVERSAL_POSITION_PCT,
                "strong_pct": settings.PAPER_REVERSAL_POSITION_PCT,
                "core_pct": settings.PAPER_REVERSAL_POSITION_PCT,
            },
            "signal_policy": {
                "buy_source": "断板反包（基准策略）",
                "candidate_filter": (
                    f"断板前连板≥{settings.PAPER_REVERSAL_MIN_CONSECUTIVE}板，断板≤{settings.PAPER_REVERSAL_MAX_GAP_DAYS}日"
                    f"且窗口内深跌≤{settings.PAPER_REVERSAL_MIN_DIP_PCT}%，反包日量比≥{settings.PAPER_REVERSAL_MIN_VOL_RATIO}，"
                    "收盘封死涨停，已开板可交易，过滤ST/停牌/退市"
                ),
                "risk_filter": "单笔硬止损仓位上限生效；回撤开仓闸门 unlimited(观测层放开)",
                "value_entry_guard": (
                    f"断板反包接力：持有≤{settings.PAPER_REVERSAL_MAX_HOLD_DAYS}日"
                    f"(独立止盈{settings.PAPER_REVERSAL_TAKE_PROFIT_PCT}%/止损{settings.PAPER_REVERSAL_STOP_LOSS_PCT}%)"
                ),
                "empty_position_policy": "每日滚动扫描涨停池断板反包形态；连板/深跌/放量不满足时不强迫成交",
                "sell_guard": (
                    f"策略F断板反包卖出：止盈{settings.PAPER_REVERSAL_TAKE_PROFIT_PCT}%/硬止损{settings.PAPER_REVERSAL_STOP_LOSS_PCT}%/"
                    f"到期{settings.PAPER_REVERSAL_MAX_HOLD_DAYS}日平仓"
                ),
            },
            "short_trade_rules": {
                "take_profit_pct": settings.PAPER_REVERSAL_TAKE_PROFIT_PCT,
                "hard_stop_loss_pct": settings.PAPER_REVERSAL_STOP_LOSS_PCT,
                "small_stop_loss_pct": settings.PAPER_REVERSAL_STOP_LOSS_PCT,
                "max_hold_days": settings.PAPER_REVERSAL_MAX_HOLD_DAYS,
            },
        }
    return None


def _auto_log_account_scope_filter(account_name: str, account_id: int):
    """自动日志的账户范围过滤.

    多账户并行:
      - default(策略A): 兼容历史 NULL 日志 + 自身日志 (历史库无 account_id)
      - 其他基准与 Challenger 账户: 只看自身日志, 不混入历史/其他策略日志
    返回一个 SQLAlchemy 布尔条件.
    """
    if account_name == PAPER_ACCOUNT_DEFAULT:
        return (PaperAutoTradeLog.account_id.is_(None)) | (
            PaperAutoTradeLog.account_id == account_id
        )
    # B-F、隔离 Challenger 以及任何显式命名账户都只能看到自身日志。
    return PaperAutoTradeLog.account_id == account_id


@router.get("/auto/logs")
async def paper_auto_logs(
    limit: int = Query(100, ge=1, le=500),
    today_only: bool = Query(False, description="仅返回今日自动执行日志"),
    account_name: str = Query(PAPER_ACCOUNT_DEFAULT, description="账户名: default=策略A / promotion=策略B"),
    db: AsyncSession = Depends(get_db),
):
    """自动模拟交易执行日志"""
    account = await _get_or_create_account(db, account_name)
    stmt = select(PaperAutoTradeLog)
    stmt = stmt.where(_auto_log_account_scope_filter(account_name, account.id),
                      PaperAutoTradeLog.action.notin_(("buy_signal", "signal_push")))
    if today_only:
        stmt = stmt.where(PaperAutoTradeLog.trade_date == date.today())
    result = await db.execute(
        stmt.order_by(desc(PaperAutoTradeLog.created_at), desc(PaperAutoTradeLog.id))
        .limit(limit)
    )
    return {"logs": [_auto_log_payload(item) for item in result.scalars().all()]}


@router.get("/auto/evaluation")
async def paper_auto_evaluation(
    account_name: str = Query(PAPER_ACCOUNT_DEFAULT, description="账户名: default=策略A / promotion=策略B"),
    db: AsyncSession = Depends(get_db),
):
    """自动交易评估 — 按买入/卖出/T减仓/T回补拆分统计."""
    account = await _get_or_create_account(db, account_name)
    await _refresh_account(db, account)

    logs = (
        await db.execute(
            select(PaperAutoTradeLog)
            .where(
                PaperAutoTradeLog.decision == "executed",
                _auto_log_account_scope_filter(account_name, account.id),
            )
            .order_by(PaperAutoTradeLog.created_at, PaperAutoTradeLog.id)
        )
    ).scalars().all()
    trade_rows = (
        await db.execute(
            select(PaperTradeLog)
            .where(
                PaperTradeLog.account_id == account.id,
                or_(
                    PaperTradeLog.excluded_from_performance.is_(False),
                    PaperTradeLog.excluded_from_performance.is_(None),
                ),
            )
            .order_by(PaperTradeLog.trade_time, PaperTradeLog.id)
        )
    ).scalars().all()
    trade_by_id = {trade.id: trade for trade in trade_rows}
    open_positions = {position.code: position for position in await _open_positions(db, account.id)}

    buckets = {
        "auto_buy": _empty_eval_bucket("自动买入"),
        "auto_sell": _empty_eval_bucket("自动卖出"),
        "t_sell": _empty_eval_bucket("T减仓"),
        "t_buyback": _empty_eval_bucket("T回补"),
    }
    samples: dict[str, list[dict]] = {key: [] for key in buckets}
    source_samples: dict[str, list[dict]] = {}
    wrong_buys = []
    wrong_sells = []

    # 误买/误卖诊断必须与每个账户实际执行的止损/止盈参数一致；旧代码
    # 只区分B与“其他”，会把C/D/E/F全部错套成A阈值。
    evaluation_params = _strategy_sell_params_by_name(account_name)
    wrong_buy_stop_pct = float(
        evaluation_params.get(
            "small_stop_loss_pct",
            evaluation_params.get(
                "stop_loss_pct",
                settings.PAPER_AUTO_SMALL_STOP_LOSS_PCT,
            ),
        )
    )
    wrong_sell_still_profit_pct = float(
        evaluation_params.get(
            "take_profit_pct",
            settings.PAPER_AUTO_TAKE_PROFIT_PCT,
        )
    )

    buy_trades = [trade for trade in trade_rows if trade.trade_type == "buy"]
    sell_trades = [trade for trade in trade_rows if trade.trade_type == "sell"]

    for log in logs:
        trade = trade_by_id.get(log.executed_trade_id or 0)
        if not trade:
            continue
        reason = log.reason or ""
        if log.action == "buy" and log.source == "position-t":
            event_type = "t_buyback"
        elif log.action == "buy":
            event_type = "auto_buy"
        elif log.action == "sell" and reason.startswith("T减仓"):
            event_type = "t_sell"
        elif log.action == "sell":
            event_type = "auto_sell"
        else:
            continue

        base_value = float(trade.price or 0) * int(trade.amount or 0)
        pnl = float(trade.realized_pnl or 0)
        return_pct = round(pnl / base_value * 100, 2) if base_value else None
        hold_days = None

        if event_type in {"auto_buy", "t_buyback"}:
            next_buy_time = next(
                (
                    item.trade_time for item in buy_trades
                    if item.code == trade.code and item.trade_time > trade.trade_time
                ),
                None,
            )
            later_sells = [
                item for item in sell_trades
                if item.code == trade.code
                and item.trade_time > trade.trade_time
                and (next_buy_time is None or item.trade_time < next_buy_time)
            ]
            realized = sum(float(item.realized_pnl or 0) for item in later_sells)
            position = open_positions.get(trade.code)
            unrealized = 0.0
            if position and position.buy_amount:
                unrealized = float(position.profit_loss or 0)
            pnl = round(realized + unrealized, 2)
            return_pct = round(pnl / base_value * 100, 2) if base_value else None
            end_time = later_sells[-1].trade_time if later_sells else datetime.now()
            hold_days = await _trade_day_hold_days(trade.trade_time.date(), end_time.date())
            if event_type == "auto_buy" and return_pct is not None and return_pct <= -wrong_buy_stop_pct:
                wrong_buys.append({
                    "code": trade.code,
                    "time": trade.trade_time.isoformat(sep=" "),
                    "price": trade.price,
                    "amount": trade.amount,
                    "return_pct": return_pct,
                    "reason": "自动买入后亏损达到小止损阈值",
                })
        else:
            previous_buy = next(
                (
                    item for item in reversed(buy_trades)
                    if item.code == trade.code and item.trade_time < trade.trade_time
                ),
                None,
            )
            if previous_buy:
                hold_days = await _trade_day_hold_days(
                    previous_buy.trade_time.date(),
                    trade.trade_time.date(),
                )
            position = open_positions.get(trade.code)
            if event_type in {"auto_sell", "t_sell"} and position and float(position.profit_pct or 0) >= wrong_sell_still_profit_pct:
                wrong_sells.append({
                    "code": trade.code,
                    "time": trade.trade_time.isoformat(sep=" "),
                    "price": trade.price,
                    "amount": trade.amount,
                    "pnl": pnl,
                    "current_profit_pct": position.profit_pct,
                    "reason": f"卖出后仍有持仓浮盈超过{wrong_sell_still_profit_pct:.0f}%，疑似过早卖出",
                })

        sample = {
            "code": trade.code,
            "time": trade.trade_time.isoformat(sep=" "),
            "price": trade.price,
            "amount": trade.amount,
            "pnl": round(pnl, 2),
            "return_pct": return_pct,
            "hold_days": hold_days,
            "reason": reason,
            "source": log.source or "",
        }
        samples[event_type].append(sample)
        source_samples.setdefault(log.source or "unknown", []).append(sample)

    stats = {
        key: _finalize_eval_bucket(buckets[key], samples[key])
        for key in buckets
    }
    all_samples = [item for group in samples.values() for item in group]
    overall = _finalize_eval_bucket(_empty_eval_bucket("全部自动交易"), all_samples)

    return {
        "account_id": account.id,
        "generated_at": datetime.now().isoformat(sep=" "),
        "diagnostic_thresholds": {
            "wrong_buy_loss_pct": wrong_buy_stop_pct,
            "wrong_sell_remaining_profit_pct": wrong_sell_still_profit_pct,
            "strategy_version": _strategy_version(account_name),
        },
        "overall": overall,
        "stats": stats,
        "source_stats": {
            key: _finalize_eval_bucket(_empty_eval_bucket(key), value)
            for key, value in source_samples.items()
        },
        "samples": {key: value[-20:] for key, value in samples.items()},
        "mistakes": {
            "wrong_buys": wrong_buys[-20:],
            "wrong_sells": wrong_sells[-20:],
        },
        "notes": [
            "自动买入/T回补在未完全卖出时会计入当前浮盈，属于动态评估。",
            "误买/误卖为规则标记样本，不直接等同于真实错误，需要人工复盘确认。",
            "在线误卖标记只覆盖卖出后仍有剩余持仓的动态浮盈；完全清仓后的卖飞必须用成交后分时回放复核，不能用空值当作未卖飞。",
        ],
    }


def _drawdown_backtest_summary(samples: list[dict]) -> dict:
    returns = [float(item["return_pct"]) for item in samples]
    pnls = [float(item["pnl"]) for item in samples]
    adverse = [float(item["max_adverse_pct"]) for item in samples]
    favorable = [float(item["max_favorable_pct"]) for item in samples]
    count = len(samples)
    return {
        "count": count,
        "wins": sum(1 for value in returns if value > 0),
        "losses": sum(1 for value in returns if value <= 0),
        "win_rate": round(sum(1 for value in returns if value > 0) / count * 100, 2) if count else 0.0,
        "avg_return_pct": round(sum(returns) / count, 2) if count else 0.0,
        "median_return_pct": round(sorted(returns)[count // 2], 2) if count else 0.0,
        "total_pnl": round(sum(pnls), 2),
        "avg_max_adverse_pct": round(sum(adverse) / count, 2) if count else 0.0,
        "avg_max_favorable_pct": round(sum(favorable) / count, 2) if count else 0.0,
    }


async def _paper_drawdown_pause_backtest(
    db: AsyncSession,
    *,
    holding_days: int = 3,
) -> dict:
    """复盘历史上被回撤锁拦截的信号；每个交易日只取评分最高的一只、固定一手。

    这是暂停策略的机会成本审计，不用结果反写实时模型，也不将日后K线用于实时信号。
    """
    paused_logs = (
        await db.execute(
            select(PaperAutoTradeLog)
            .where(
                PaperAutoTradeLog.action == "skip_buy",
                PaperAutoTradeLog.decision.in_(["skipped", "blocked"]),
                PaperAutoTradeLog.code.is_not(None),
                PaperAutoTradeLog.price > 0,
                PaperAutoTradeLog.reason.like("%账户当前回撤%"),
            )
            .order_by(
                PaperAutoTradeLog.trade_date,
                desc(PaperAutoTradeLog.candidate_score),
                PaperAutoTradeLog.created_at,
                PaperAutoTradeLog.id,
            )
        )
    ).scalars().all()

    # 调度任务一天会重复评估同一候选；按日只保留评分最高的一只，避免把轮询次数
    # 错当成可交易样本数量。
    top_by_day: dict[date, PaperAutoTradeLog] = {}
    for log in paused_logs:
        if log.trade_date not in top_by_day:
            top_by_day[log.trade_date] = log
    selected_logs = list(top_by_day.values())
    if not selected_logs:
        return {
            "holding_days": holding_days,
            "paused_signal_days": 0,
            "completed_samples": 0,
            "pause_open": {"count": 0, "total_pnl": 0.0},
            "continuous_one_lot": _drawdown_backtest_summary([]),
            "samples": [],
            "notes": ["暂无带价格的回撤暂停日志，无法审计暂停开仓的机会成本。"],
        }

    codes = {str(log.code) for log in selected_logs if log.code}
    first_signal_date = min(log.trade_date for log in selected_logs)
    klines = (
        await db.execute(
            select(StockKline)
            .where(
                StockKline.code.in_(codes),
                StockKline.trade_date > first_signal_date,
            )
            .order_by(StockKline.code, StockKline.trade_date)
        )
    ).scalars().all()
    kline_by_code: dict[str, list[StockKline]] = {}
    for row in klines:
        kline_by_code.setdefault(row.code, []).append(row)

    samples: list[dict] = []
    for log in selected_logs:
        entry_price = float(log.price or 0)
        future_rows = [
            row
            for row in kline_by_code.get(str(log.code), [])
            if row.trade_date > log.trade_date and row.close and row.low and row.high
        ][:holding_days]
        if entry_price <= 0 or len(future_rows) < holding_days:
            continue
        exit_row = future_rows[-1]
        exit_price = float(exit_row.close or 0)
        amount = 100
        buy_value = entry_price * amount
        sell_value = exit_price * amount
        pnl = (
            sell_value
            - buy_value
            - _commission(buy_value)
            - _commission(sell_value)
            - _stamp_tax(sell_value)
        )
        return_pct = pnl / buy_value * 100 if buy_value else 0.0
        samples.append({
            "trade_date": log.trade_date.isoformat(),
            "exit_date": exit_row.trade_date.isoformat(),
            "code": log.code,
            "name": log.name or "",
            "source": log.source or "",
            "score": round(float(log.candidate_score or 0), 2),
            "entry_price": round(entry_price, 2),
            "exit_price": round(exit_price, 2),
            "amount": amount,
            "pnl": round(pnl, 2),
            "return_pct": round(return_pct, 2),
            "max_adverse_pct": round(
                min(float(row.low) / entry_price - 1 for row in future_rows) * 100,
                2,
            ),
            "max_favorable_pct": round(
                max(float(row.high) / entry_price - 1 for row in future_rows) * 100,
                2,
            ),
        })

    summary = _drawdown_backtest_summary(samples)
    return {
        "holding_days": holding_days,
        "paused_signal_days": len(selected_logs),
        "completed_samples": len(samples),
        "pause_open": {
            "count": len(samples),
            "total_pnl": 0.0,
            "description": "历史实际策略：回撤锁触发后不开仓",
        },
        "continuous_one_lot": {
            **summary,
            "description": "反事实策略：每日评分最高的被拦候选买100股，持有固定交易日后收盘卖出",
        },
        "policy_delta_pnl": summary["total_pnl"],
        "samples": samples[-30:],
        "notes": [
            "仅使用信号日之后的K线计算收益，不参与实时信号，避免未来函数。",
            "历史库未保存完整分钟盘口，因此这是回撤暂停规则的机会成本审计，不等同于新版盘面观察算法的严格分钟级回测。",
            "结果已计项目当前佣金和卖出印花税；一手固定为100股。",
        ],
    }


@router.get("/auto/drawdown-backtest")
async def paper_auto_drawdown_backtest(
    holding_days: int = Query(3, ge=1, le=10),
    db: AsyncSession = Depends(get_db),
):
    """审计“回撤后暂停开仓”相对于每日一手持续观察的历史机会成本。"""
    return await _paper_drawdown_pause_backtest(db, holding_days=holding_days)


@router.post("/auto/run")
async def paper_auto_run(
    req: AutoRunRequest,
    account_name: str = Query(PAPER_ACCOUNT_DEFAULT, description="账户名: default=策略A / promotion=策略B"),
    db: AsyncSession = Depends(get_db),
):
    """立即运行一次自动模拟交易。execute=false 时只记录演练结果，不下单。"""
    token = None
    if req.execution_mode == "intraday" and req.execute:
        from app.data.quote_round import load_latest_healthy_quote_payload

        payload = await load_latest_healthy_quote_payload(
            db,
            trade_day=_paper_now().date(),
            now=_paper_now(),
        )
        if payload is None:
            raise HTTPException(
                status_code=409,
                detail="没有仍新鲜且明细完整的QuoteRound，自动委托失败关闭",
            )
        token = _QUOTE_ROUND_CONTEXT.set(payload)
    try:
        return await run_paper_auto_trade(
            db,
            execute=req.execute,
            trigger=req.trigger or "manual",
            max_candidates=req.max_candidates,
            execution_mode=req.execution_mode,
            account_name=account_name,
        )
    finally:
        if token is not None:
            _QUOTE_ROUND_CONTEXT.reset(token)


@router.get("/shadow/momentum-retest")
async def paper_momentum_retest_shadow(
    trade_date: Optional[date] = Query(None),
    limit: int = Query(100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
):
    """查看3%~6%强势股首次回踩路线的前向影子事件与晋级证据。"""
    from app.paper.momentum_retest_shadow import (
        get_momentum_retest_shadow_dashboard,
    )

    return await get_momentum_retest_shadow_dashboard(
        db,
        trade_date=trade_date,
        limit=limit,
    )
