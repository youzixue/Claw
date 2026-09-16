"""3%~6%强势股首次回踩确认的前向影子路线。

该模块只记录候选、首次回踩和确认时点，绝不返回模拟盘买单，也不调用交易链路。
历史日K无法恢复日内先后顺序，因此只允许做覆盖审计；真正晋级证据来自本表前向积累。
"""

from __future__ import annotations

import json
import math
import statistics
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any, Iterable, Mapping

from loguru import logger
from sqlalchemy import func, or_, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import settings
from app.models.paper import PaperShadowEvaluation, PaperShadowEvent
from app.models.stock import StockBlacklist, StockKline, StockTag


ROUTE_ID = "momentum_first_retest"
TERMINAL_STAGES = {"confirmed", "invalidated", "expired", "coverage_blocked"}
# 一轮全市场首次启动会同时产生三千余条 armed 事件。SQLite 默认变量上限
# 低于一次多行 INSERT 所需的列数乘行数，必须分批追加，否则整条前向审计链
# 会因 ``too many SQL variables`` 从开盘起持续失败。
SHADOW_EVENT_INSERT_BATCH_SIZE = 50


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def _parse_hhmm(value: str, fallback: time) -> time:
    try:
        hour, minute = str(value).split(":", 1)
        return time(int(hour), int(minute))
    except (TypeError, ValueError):
        return fallback


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def _round(value: Any, digits: int = 4) -> float:
    return round(_safe_float(value), digits)


def _json_loads(value: str | None) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


MOMENTUM_LIQUIDITY_CONTRACT_VERSION = "momentum_liquidity_v1"


def momentum_liquidity_gate_issues(
    quote: Mapping[str, Any],
    rules: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Shared candidate/confirmation/execution leaves; missing is never neutral.

    Full new events carry frozen rules; legacy partial snapshots keep the
    existing settings fallback, without treating a valid zero as missing.
    """
    defaults = {
        "min_volume_ratio": settings.PAPER_MOMENTUM_RETEST_MIN_VOLUME_RATIO,
        "max_volume_ratio": settings.PAPER_MOMENTUM_RETEST_MAX_VOLUME_RATIO,
        "min_amount": settings.PAPER_MOMENTUM_RETEST_MIN_AMOUNT,
        "min_orderbook_imbalance": settings.PAPER_MOMENTUM_RETEST_MIN_ORDERBOOK_IMBALANCE,
        "max_withdrawal_ratio": settings.PAPER_MOMENTUM_RETEST_MAX_WITHDRAWAL_RATIO,
    }
    limits = {key: _safe_float(rules.get(key, default), math.nan) for key, default in defaults.items()}
    if (not all(math.isfinite(value) for value in limits.values())
            or not 0 <= limits["min_volume_ratio"] <= limits["max_volume_ratio"]
            or limits["min_amount"] < 0 or limits["max_withdrawal_ratio"] < 0):
        return [{"code": "invalid_rule_snapshot", "reason": "首次回踩流动性规则无效，禁止确认或执行",
                 "recoverable": False}]

    issues = []
    for field_name, label, lower, upper in (
        ("volume_ratio", "量比", limits["min_volume_ratio"], limits["max_volume_ratio"]),
        ("amount", "累计成交额", limits["min_amount"], math.inf),
        ("orderbook_imbalance", "盘口失衡", limits["min_orderbook_imbalance"], math.inf),
        ("withdrawal_ratio", "撤单比例", 0.0, limits["max_withdrawal_ratio"]),
    ):
        value = _safe_float(quote.get(field_name), math.nan)
        if not math.isfinite(value):
            issues.append({"code": f"missing_{field_name}", "reason": f"暂缺有效{label}证据",
                           "recoverable": True})
        elif value < lower or value > upper:
            issues.append({"code": f"out_of_range_{field_name}",
                           "reason": "累计成交额不足" if field_name == "amount" else f"{label}不在首次回踩规则区间",
                           "recoverable": False})
    return issues


@dataclass(frozen=True)
class MomentumRetestPolicy:
    version: str
    start: time
    candidate_end: time
    confirm_end: time
    rearm_max_change_pct: float
    candidate_min_change_pct: float
    candidate_max_change_pct: float
    min_volume_ratio: float
    max_volume_ratio: float
    min_amount: float
    min_pullback_pct: float
    max_pullback_pct: float
    min_hold_change_pct: float
    min_recovery_pct: float
    max_peak_gap_pct: float
    min_60s_change_pct: float
    min_amount_pace_ratio: float
    min_orderbook_imbalance: float
    max_withdrawal_ratio: float
    max_vwap_break_pct: float
    max_quote_gap_sec: int
    min_track_sec: int
    max_track_sec: int
    max_confirm_wait_sec: int

    @classmethod
    def from_settings(cls) -> "MomentumRetestPolicy":
        return cls(
            version=str(settings.PAPER_MOMENTUM_RETEST_SHADOW_VERSION),
            start=_parse_hhmm(settings.PAPER_MOMENTUM_RETEST_START, time(9, 35)),
            candidate_end=_parse_hhmm(
                settings.PAPER_MOMENTUM_RETEST_CANDIDATE_END,
                time(14, 30),
            ),
            confirm_end=_parse_hhmm(
                settings.PAPER_MOMENTUM_RETEST_CONFIRM_END,
                time(14, 50),
            ),
            rearm_max_change_pct=_safe_float(
                settings.PAPER_MOMENTUM_RETEST_REARM_MAX_CHANGE_PCT,
                2.5,
            ),
            candidate_min_change_pct=_safe_float(
                settings.PAPER_MOMENTUM_RETEST_CANDIDATE_MIN_CHANGE_PCT,
                3.0,
            ),
            candidate_max_change_pct=_safe_float(
                settings.PAPER_MOMENTUM_RETEST_CANDIDATE_MAX_CHANGE_PCT,
                6.0,
            ),
            min_volume_ratio=_safe_float(
                settings.PAPER_MOMENTUM_RETEST_MIN_VOLUME_RATIO,
                0.8,
            ),
            max_volume_ratio=_safe_float(
                settings.PAPER_MOMENTUM_RETEST_MAX_VOLUME_RATIO,
                5.0,
            ),
            min_amount=_safe_float(settings.PAPER_MOMENTUM_RETEST_MIN_AMOUNT),
            min_pullback_pct=_safe_float(
                settings.PAPER_MOMENTUM_RETEST_MIN_PULLBACK_PCT,
                0.5,
            ),
            max_pullback_pct=_safe_float(
                settings.PAPER_MOMENTUM_RETEST_MAX_PULLBACK_PCT,
                1.8,
            ),
            min_hold_change_pct=_safe_float(
                settings.PAPER_MOMENTUM_RETEST_MIN_HOLD_CHANGE_PCT,
                2.5,
            ),
            min_recovery_pct=_safe_float(
                settings.PAPER_MOMENTUM_RETEST_MIN_RECOVERY_PCT,
                0.3,
            ),
            max_peak_gap_pct=_safe_float(
                settings.PAPER_MOMENTUM_RETEST_MAX_PEAK_GAP_PCT,
                0.8,
            ),
            min_60s_change_pct=_safe_float(
                settings.PAPER_MOMENTUM_RETEST_MIN_60S_CHANGE_PCT,
                0.15,
            ),
            min_amount_pace_ratio=_safe_float(
                settings.PAPER_MOMENTUM_RETEST_MIN_AMOUNT_PACE_RATIO,
                0.8,
            ),
            min_orderbook_imbalance=_safe_float(
                settings.PAPER_MOMENTUM_RETEST_MIN_ORDERBOOK_IMBALANCE,
                -0.2,
            ),
            max_withdrawal_ratio=_safe_float(
                settings.PAPER_MOMENTUM_RETEST_MAX_WITHDRAWAL_RATIO,
                0.5,
            ),
            max_vwap_break_pct=_safe_float(
                settings.PAPER_MOMENTUM_RETEST_MAX_VWAP_BREAK_PCT,
                0.2,
            ),
            max_quote_gap_sec=max(
                int(settings.PAPER_MOMENTUM_RETEST_MAX_QUOTE_GAP_SEC),
                1,
            ),
            min_track_sec=max(
                int(settings.PAPER_MOMENTUM_RETEST_MIN_TRACK_SEC),
                0,
            ),
            max_track_sec=max(
                int(settings.PAPER_MOMENTUM_RETEST_MAX_TRACK_SEC),
                1,
            ),
            max_confirm_wait_sec=max(
                int(settings.PAPER_MOMENTUM_RETEST_MAX_CONFIRM_WAIT_SEC),
                1,
            ),
        )

    def snapshot(self) -> dict[str, Any]:
        raw = asdict(self)
        raw["start"] = self.start.strftime("%H:%M")
        raw["candidate_end"] = self.candidate_end.strftime("%H:%M")
        raw["confirm_end"] = self.confirm_end.strftime("%H:%M")
        return raw


@dataclass
class MomentumRetestState:
    code: str
    name: str
    trade_date: date
    stage: str = "unseen"
    saw_below_rearm: bool = False
    band_seen: bool = False
    band_seen_before_start: bool = False
    observation_count: int = 0
    last_at: datetime | None = None
    last_price: float = 0.0
    last_amount: float = 0.0
    candidate_at: datetime | None = None
    candidate_price: float = 0.0
    candidate_change_pct: float = 0.0
    peak_at: datetime | None = None
    peak_price: float = 0.0
    peak_change_pct: float = 0.0
    pullback_at: datetime | None = None
    trough_at: datetime | None = None
    trough_price: float = 0.0
    trough_change_pct: float = 0.0
    terminal_reason: str = ""
    event_types: set[str] = field(default_factory=set)
    quote_history: list[tuple[datetime, float, float]] = field(default_factory=list)

    def snapshot(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "name": self.name,
            "trade_date": self.trade_date.isoformat(),
            "stage": self.stage,
            "saw_below_rearm": self.saw_below_rearm,
            "band_seen": self.band_seen,
            "band_seen_before_start": self.band_seen_before_start,
            "observation_count": self.observation_count,
            "last_at": self.last_at.isoformat() if self.last_at else None,
            "last_price": _round(self.last_price),
            "last_amount": _round(self.last_amount, 2),
            "candidate_at": self.candidate_at.isoformat() if self.candidate_at else None,
            "candidate_price": _round(self.candidate_price),
            "candidate_change_pct": _round(self.candidate_change_pct, 3),
            "peak_at": self.peak_at.isoformat() if self.peak_at else None,
            "peak_price": _round(self.peak_price),
            "peak_change_pct": _round(self.peak_change_pct, 3),
            "pullback_at": self.pullback_at.isoformat() if self.pullback_at else None,
            "trough_at": self.trough_at.isoformat() if self.trough_at else None,
            "trough_price": _round(self.trough_price),
            "trough_change_pct": _round(self.trough_change_pct, 3),
            "terminal_reason": self.terminal_reason,
            "event_types": sorted(self.event_types),
            "quote_history": [
                [item_at.isoformat(), _round(price), _round(amount, 2)]
                for item_at, price, amount in self.quote_history[-20:]
            ],
        }

    @classmethod
    def from_snapshot(cls, payload: Mapping[str, Any]) -> "MomentumRetestState" | None:
        try:
            trade_date = date.fromisoformat(str(payload.get("trade_date") or ""))
        except ValueError:
            return None
        state = cls(
            code=str(payload.get("code") or ""),
            name=str(payload.get("name") or ""),
            trade_date=trade_date,
            stage=str(payload.get("stage") or "unseen"),
            saw_below_rearm=bool(payload.get("saw_below_rearm")),
            band_seen=bool(payload.get("band_seen")),
            band_seen_before_start=bool(payload.get("band_seen_before_start")),
            observation_count=int(payload.get("observation_count") or 0),
            last_at=_parse_datetime(payload.get("last_at")),
            last_price=_safe_float(payload.get("last_price")),
            last_amount=_safe_float(payload.get("last_amount")),
            candidate_at=_parse_datetime(payload.get("candidate_at")),
            candidate_price=_safe_float(payload.get("candidate_price")),
            candidate_change_pct=_safe_float(payload.get("candidate_change_pct")),
            peak_at=_parse_datetime(payload.get("peak_at")),
            peak_price=_safe_float(payload.get("peak_price")),
            peak_change_pct=_safe_float(payload.get("peak_change_pct")),
            pullback_at=_parse_datetime(payload.get("pullback_at")),
            trough_at=_parse_datetime(payload.get("trough_at")),
            trough_price=_safe_float(payload.get("trough_price")),
            trough_change_pct=_safe_float(payload.get("trough_change_pct")),
            terminal_reason=str(payload.get("terminal_reason") or ""),
            event_types={str(item) for item in payload.get("event_types") or []},
        )
        for item in payload.get("quote_history") or []:
            if not isinstance(item, list) or len(item) != 3:
                continue
            item_at = _parse_datetime(item[0])
            if item_at is not None:
                state.quote_history.append(
                    (item_at, _safe_float(item[1]), _safe_float(item[2]))
                )
        return state if state.code else None


class MomentumRetestShadowEngine:
    """只消费按时点到达的行情快照，输出追加式影子事件。"""

    def __init__(self, policy: MomentumRetestPolicy | None = None):
        self.policy = policy or MomentumRetestPolicy.from_settings()
        self._trade_date: date | None = None
        self._states: dict[str, MomentumRetestState] = {}
        self._pending: dict[str, dict[str, Any]] = {}
        self._consumer_coverage_loss: dict[str, Any] | None = None

    @property
    def states(self) -> Mapping[str, MomentumRetestState]:
        return self._states

    def _reset_for(self, trade_date: date) -> None:
        if self._trade_date == trade_date:
            return
        self._trade_date = trade_date
        self._states.clear()
        self._pending.clear()
        self._consumer_coverage_loss = None

    def restore(self, events: Iterable[PaperShadowEvent]) -> None:
        rows = list(events)
        if not rows:
            return
        self._reset_for(rows[0].trade_date)
        for row in rows:
            payload = _json_loads(row.snapshot_json)
            loss = (payload.get("extra") or {}).get("consumer_coverage_loss")
            if isinstance(loss, dict) and loss:
                self._consumer_coverage_loss = dict(loss)
            state = MomentumRetestState.from_snapshot(payload.get("state") or {})
            if state is None:
                continue
            state.event_types.add(str(row.event_type))
            self._states[state.code] = state
        # armed 只证明此前真实观察到涨幅不高于武装线，尚未进入候选/回踩，
        # 可在下一轮用 quote_gap 再校验连续性；candidate/pullback 的“首次”路径
        # 依赖事件间逐帧峰低点，重启后仍必须失败关闭。
        for state in self._states.values():
            if state.stage in {"candidate", "pullback", "interrupted"}:
                state.stage = "interrupted"
                state.terminal_reason = "进程重启导致连续行情路径中断，不能继续证明首次回踩"

    def pending_events(self) -> list[dict[str, Any]]:
        return list(self._pending.values())

    def ack(self, event_keys: Iterable[str]) -> None:
        for key in event_keys:
            self._pending.pop(str(key), None)

    def observe_batch(
        self,
        quotes: Iterable[Mapping[str, Any]],
        observed_at: datetime,
        allowed_codes: set[str],
        *,
        coverage_loss: Mapping[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        self._reset_for(observed_at.date())
        if coverage_loss:
            # 已知丢帧不能靠剩余两帧间隔较短恢复“首次”证明；当天后到股票也受约束。
            self._consumer_coverage_loss = dict(coverage_loss)
        for quote in quotes:
            code = str(quote.get("code") or "")
            if code not in allowed_codes:
                continue
            source_quote_at = _parse_datetime(quote.get("source_quote_at"))
            if source_quote_at is not None and (
                source_quote_at.date() != observed_at.date()
                or source_quote_at
                > observed_at
                + timedelta(
                    seconds=max(1, int(settings.ANOMALY_QUOTE_ROUND_TOLERANCE_SEC))
                )
            ):
                # 显式源时点跨日或明显来自未来时不能伪装成当前提交时点。
                continue
            quote_observed_at = (
                source_quote_at
                or _parse_datetime(quote.get("received_at"))
                or _parse_datetime(quote.get("updated_at"))
                or observed_at
            )
            if quote_observed_at.date() != observed_at.date():
                continue
            if self._consumer_coverage_loss:
                state = self._states.get(code)
                if state is None:
                    state = MomentumRetestState(
                        code=code, name=str(quote.get("name") or code),
                        trade_date=observed_at.date(),
                    )
                    self._states[code] = state
                if state.stage not in TERMINAL_STAGES:
                    state.stage = "coverage_blocked"
                    state.terminal_reason = "已采集轮次消费丢失或质量降级，不能继续证明当日首次回踩"
                    self._emit(state, quote, observed_at, "coverage_blocked", extra={
                        "reason": state.terminal_reason,
                        "point_in_time_coverage": "consumer_coverage_loss",
                        "consumer_coverage_loss": dict(self._consumer_coverage_loss),
                    })
                continue
            self.observe_quote(quote, quote_observed_at)
        return self.pending_events()

    def observe_quote(
        self,
        quote: Mapping[str, Any],
        observed_at: datetime,
    ) -> None:
        code = str(quote.get("code") or "")
        name = str(quote.get("name") or code)
        price = _safe_float(quote.get("price"))
        prev_close = _safe_float(quote.get("prev_close"))
        amount = _safe_float(quote.get("amount"))
        change_pct = _safe_float(quote.get("change_pct"))
        if not code or price <= 0 or prev_close <= 0 or amount < 0:
            return

        state = self._states.get(code)
        if state is None:
            state = MomentumRetestState(code=code, name=name, trade_date=observed_at.date())
            self._states[code] = state
        if state.last_at is not None and observed_at <= state.last_at:
            return
        if state.stage in TERMINAL_STAGES:
            return
        previous_at = state.last_at
        quote_gap_sec = (
            max(
                self._trading_elapsed_seconds(observed_at)
                - self._trading_elapsed_seconds(previous_at),
                0.0,
            )
            if previous_at is not None and previous_at.date() == observed_at.date()
            else 0.0
        )

        state.name = name
        state.observation_count += 1
        state.last_at = observed_at
        state.last_price = price
        state.last_amount = amount
        state.quote_history.append((observed_at, price, amount))
        cutoff = observed_at - timedelta(minutes=8)
        state.quote_history[:] = [item for item in state.quote_history if item[0] >= cutoff]

        if state.stage == "interrupted":
            state.stage = "coverage_blocked"
            self._emit(state, quote, observed_at, "coverage_blocked", extra={
                "reason": state.terminal_reason,
                "point_in_time_coverage": "interrupted_by_restart",
            })
            return
        if (
            quote_gap_sec > self.policy.max_quote_gap_sec
            and (
                observed_at.time() <= self.policy.candidate_end
                or state.stage in {"candidate", "pullback"}
            )
        ):
            state.stage = "coverage_blocked"
            state.terminal_reason = "连续行情快照间隔过长，期间路径不可见，不能证明首次回踩"
            self._emit(state, quote, observed_at, "coverage_blocked", extra={
                "reason": state.terminal_reason,
                "point_in_time_coverage": "quote_gap",
                "quote_gap_sec": _round(quote_gap_sec, 1),
                "max_quote_gap_sec": self.policy.max_quote_gap_sec,
            })
            return

        if change_pct <= self.policy.rearm_max_change_pct:
            state.saw_below_rearm = True
            if state.stage == "unseen":
                state.stage = "armed"
                self._emit(state, quote, observed_at, "armed", extra={
                    "reason": "已真实观察到涨幅不高于武装线，进入当日3%~6%候选池",
                    "automatic_order_connected": False,
                })

        if observed_at.time() < self.policy.start:
            if change_pct >= self.policy.candidate_min_change_pct:
                state.band_seen_before_start = True
            return
        if state.band_seen_before_start:
            state.stage = "coverage_blocked"
            state.terminal_reason = "09:35前已进入3%强势区，不能把开盘噪声后的状态冒充首次候选"
            self._emit(state, quote, observed_at, "coverage_blocked", extra={
                "reason": state.terminal_reason,
                "point_in_time_coverage": "entered_band_before_route_start",
            })
            return
        if (
            observed_at.time() > self.policy.candidate_end
            and state.stage in {"unseen", "armed"}
        ):
            # 候选窗口关闭后只允许既有candidate/pullback继续确认，
            # 不再把盘尾新出现的强势股记成覆盖缺口或新候选。
            return

        if state.stage == "unseen" and change_pct >= self.policy.candidate_min_change_pct:
            state.stage = "coverage_blocked"
            state.terminal_reason = "启动后未观察到3%以下路径，无法证明这是当日首次回踩"
            self._emit(state, quote, observed_at, "coverage_blocked", extra={
                "reason": state.terminal_reason,
                "point_in_time_coverage": "insufficient",
            })
            return

        if state.stage == "armed":
            self._observe_armed(state, quote, observed_at, change_pct)
            return

        if state.stage in {"candidate", "pullback"}:
            self._observe_tracking(state, quote, observed_at, change_pct)

    def _observe_armed(
        self,
        state: MomentumRetestState,
        quote: Mapping[str, Any],
        observed_at: datetime,
        change_pct: float,
    ) -> None:
        if observed_at.time() > self.policy.candidate_end:
            return
        in_band = (
            self.policy.candidate_min_change_pct
            <= change_pct
            <= self.policy.candidate_max_change_pct
        )
        if in_band:
            state.band_seen = True
            gate_reasons = self._candidate_gate_reasons(quote)
            if gate_reasons:
                # 首次进入候选带时保留漏斗原因；后续质量改善仍可形成候选。
                self._emit(state, quote, observed_at, "screened", extra={
                    "candidate_gate_passed": False,
                    "candidate_gate_reasons": gate_reasons,
                })
            else:
                state.stage = "candidate"
                state.candidate_at = observed_at
                state.candidate_price = _safe_float(quote.get("price"))
                state.candidate_change_pct = change_pct
                state.peak_at = observed_at
                state.peak_price = state.candidate_price
                state.peak_change_pct = change_pct
                self._emit(state, quote, observed_at, "candidate", extra={
                    "candidate_gate_passed": True,
                    "candidate_gate_reasons": [],
                })
            return
        if state.band_seen and change_pct > self.policy.candidate_max_change_pct:
            state.stage = "invalidated"
            state.terminal_reason = "首次3%~6%窗口内未通过质量门，随后已离开候选区间"
            self._emit(state, quote, observed_at, "invalidated", extra={
                "reason": state.terminal_reason,
            })
        elif state.band_seen and change_pct < self.policy.rearm_max_change_pct:
            state.stage = "invalidated"
            state.terminal_reason = "首次强势脉冲在形成合格候选前回落失效"
            self._emit(state, quote, observed_at, "invalidated", extra={
                "reason": state.terminal_reason,
            })

    def _candidate_gate_reasons(self, quote: Mapping[str, Any]) -> list[str]:
        reasons: list[str] = []
        price = _safe_float(quote.get("price"))
        avg_price = _safe_float(quote.get("avg_price"))
        ask_price = _safe_float(quote.get("ask1_price"))
        ask_volume = _safe_float(quote.get("ask1_volume"))
        limit_up = _safe_float(quote.get("limit_up"))
        if ask_price <= 0 or ask_volume <= 0:
            reasons.append("卖一无可成交量")
        if limit_up > 0 and price >= limit_up:
            reasons.append("已涨停不可追")
        if avg_price <= 0 or price < avg_price:
            reasons.append("初始脉冲未站稳VWAP")
        reasons.extend(issue["reason"] for issue in momentum_liquidity_gate_issues(
            quote, self.policy.snapshot(),
        ))
        return reasons

    def _observe_tracking(
        self,
        state: MomentumRetestState,
        quote: Mapping[str, Any],
        observed_at: datetime,
        change_pct: float,
    ) -> None:
        if state.candidate_at is None or state.candidate_price <= 0:
            state.stage = "invalidated"
            state.terminal_reason = "候选状态缺少时点锚点"
            self._emit(state, quote, observed_at, "invalidated", extra={
                "reason": state.terminal_reason,
            })
            return
        price = _safe_float(quote.get("price"))
        if price > state.peak_price:
            state.peak_price = price
            state.peak_at = observed_at
            state.peak_change_pct = change_pct
        if state.stage == "pullback" and (state.trough_price <= 0 or price < state.trough_price):
            state.trough_price = price
            state.trough_at = observed_at
            state.trough_change_pct = change_pct

        age_sec = (observed_at - state.candidate_at).total_seconds()
        pullback_pct = (
            (state.peak_price / price - 1.0) * 100.0
            if state.peak_price > 0 and price > 0
            else 99.0
        )
        if observed_at.time() > self.policy.confirm_end:
            self._expire(state, quote, observed_at, "超过影子确认时段")
            return
        if age_sec > self.policy.max_track_sec:
            self._expire(state, quote, observed_at, "候选等待首次回踩超时")
            return
        if change_pct < self.policy.min_hold_change_pct:
            self._invalidate(state, quote, observed_at, "回踩后日内强度跌破保留线")
            return
        if pullback_pct > self.policy.max_pullback_pct:
            self._invalidate(state, quote, observed_at, "从候选峰值回撤过深")
            return
        if self._unfillable(quote):
            self._invalidate(state, quote, observed_at, "卖一无量或已封板，影子成交不可实现")
            return

        avg_price = _safe_float(quote.get("avg_price"))
        vwap_hold = bool(
            avg_price > 0
            and price >= avg_price * (1.0 - self.policy.max_vwap_break_pct / 100.0)
        )
        if (
            state.stage == "candidate"
            and age_sec >= self.policy.min_track_sec
            and self.policy.min_pullback_pct
            <= pullback_pct
            <= self.policy.max_pullback_pct
            and vwap_hold
        ):
            state.stage = "pullback"
            state.pullback_at = observed_at
            state.trough_at = observed_at
            state.trough_price = price
            state.trough_change_pct = change_pct
            self._emit(state, quote, observed_at, "pullback", extra={
                "pullback_from_peak_pct": _round(pullback_pct, 3),
                "vwap_hold": True,
            })
            return

        if state.stage != "pullback" or state.pullback_at is None:
            return
        wait_sec = (observed_at - state.pullback_at).total_seconds()
        if wait_sec > self.policy.max_confirm_wait_sec:
            self._expire(state, quote, observed_at, "首次回踩后未在限定时间内重新转强")
            return

        rolling = self._rolling_60s(state, observed_at)
        recovery_pct = (
            (price / state.trough_price - 1.0) * 100.0
            if state.trough_price > 0
            else 0.0
        )
        peak_gap_pct = (
            (state.peak_price / price - 1.0) * 100.0
            if state.peak_price > 0 and price > 0
            else 99.0
        )
        confirm_reasons: list[str] = []
        if not (
            self.policy.candidate_min_change_pct
            <= change_pct
            <= self.policy.candidate_max_change_pct
        ):
            confirm_reasons.append("确认价不在3%~6%执行区间")
        if recovery_pct < self.policy.min_recovery_pct:
            confirm_reasons.append("低点回收幅度不足")
        if peak_gap_pct > self.policy.max_peak_gap_pct:
            confirm_reasons.append("尚未收回峰值附近")
        if not vwap_hold or price < avg_price:
            confirm_reasons.append("确认时未重新站上VWAP")
        if rolling["change_pct"] < self.policy.min_60s_change_pct:
            confirm_reasons.append("60秒价格动能不足")
        if rolling["amount_pace_ratio"] < self.policy.min_amount_pace_ratio:
            confirm_reasons.append("60秒成交速率不足")
        liquidity_issues = momentum_liquidity_gate_issues(quote, self.policy.snapshot())
        confirm_reasons.extend(issue["reason"] for issue in liquidity_issues)
        if confirm_reasons:
            if liquidity_issues:
                # 记录一次未通过原因但不消耗confirmed身份；仍受原窗口/路径失效约束。
                self._emit(state, quote, observed_at, "confirmation_screened", extra={
                    "confirmation_gate_passed": False,
                    "confirmation_gate_reasons": confirm_reasons,
                    "liquidity_contract": MOMENTUM_LIQUIDITY_CONTRACT_VERSION,
                    "liquidity_issues": liquidity_issues,
                })
            return

        state.stage = "confirmed"
        state.terminal_reason = "首次回踩后重新站上VWAP并获价格、成交与盘口共同确认"
        assumed_fill = self._assumed_fill_price(quote)
        self._emit(state, quote, observed_at, "confirmed", assumed_fill_price=assumed_fill, extra={
            "reason": state.terminal_reason,
            "pullback_from_peak_pct": _round(pullback_pct, 3),
            "recovery_from_trough_pct": _round(recovery_pct, 3),
            "peak_gap_pct": _round(peak_gap_pct, 3),
            "rolling_60s": rolling,
            "execution_mode": "shadow_only",
            "automatic_order_connected": False,
        })

    def _rolling_60s(
        self,
        state: MomentumRetestState,
        observed_at: datetime,
    ) -> dict[str, float]:
        current = state.quote_history[-1]
        candidates: list[tuple[float, tuple[datetime, float, float]]] = []
        for item in state.quote_history[:-1]:
            interval = (observed_at - item[0]).total_seconds()
            if 40.0 <= interval <= 90.0:
                candidates.append((abs(interval - 60.0), item))
        if not candidates:
            return {"change_pct": 0.0, "amount_delta": 0.0, "amount_pace_ratio": 0.0}
        _, baseline = min(candidates, key=lambda item: item[0])
        interval = (current[0] - baseline[0]).total_seconds()
        amount_delta = current[2] - baseline[2]
        if baseline[1] <= 0 or interval <= 0 or amount_delta <= 0:
            return {"change_pct": 0.0, "amount_delta": 0.0, "amount_pace_ratio": 0.0}
        change_pct = (current[1] / baseline[1] - 1.0) * 100.0
        recent_rate = amount_delta / interval
        prior_rates: list[float] = []
        prior = [item for item in state.quote_history if item[0] <= baseline[0]]
        for previous, item in zip(prior, prior[1:]):
            seconds = (item[0] - previous[0]).total_seconds()
            delta = item[2] - previous[2]
            if 15.0 <= seconds <= 120.0 and delta > 0:
                prior_rates.append(delta / seconds)
        if len(prior_rates) >= 2:
            reference_rate = statistics.median(prior_rates[-5:])
        else:
            elapsed = self._trading_elapsed_seconds(baseline[0])
            reference_rate = baseline[2] / elapsed if elapsed > 0 else 0.0
        pace_ratio = recent_rate / reference_rate if reference_rate > 0 else 0.0
        return {
            "change_pct": _round(change_pct, 3),
            "amount_delta": _round(amount_delta, 2),
            "amount_pace_ratio": _round(pace_ratio, 3),
        }

    @staticmethod
    def _trading_elapsed_seconds(quote_time: datetime) -> float:
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

    @staticmethod
    def _unfillable(quote: Mapping[str, Any]) -> bool:
        price = _safe_float(quote.get("price"))
        limit_up = _safe_float(quote.get("limit_up"))
        return bool(
            _safe_float(quote.get("ask1_price")) <= 0
            or _safe_float(quote.get("ask1_volume")) <= 0
            or (limit_up > 0 and price >= limit_up)
        )

    @staticmethod
    def _assumed_fill_price(quote: Mapping[str, Any]) -> float:
        ask_price = _safe_float(quote.get("ask1_price"))
        limit_up = _safe_float(quote.get("limit_up"))
        fill = ask_price * (1.0 + max(_safe_float(settings.PAPER_EXECUTION_SLIPPAGE_PCT), 0.0) / 100.0)
        if limit_up > 0:
            fill = min(fill, limit_up)
        return _round(fill)

    def _invalidate(
        self,
        state: MomentumRetestState,
        quote: Mapping[str, Any],
        observed_at: datetime,
        reason: str,
    ) -> None:
        state.stage = "invalidated"
        state.terminal_reason = reason
        self._emit(state, quote, observed_at, "invalidated", extra={"reason": reason})

    def _expire(
        self,
        state: MomentumRetestState,
        quote: Mapping[str, Any],
        observed_at: datetime,
        reason: str,
    ) -> None:
        state.stage = "expired"
        state.terminal_reason = reason
        self._emit(state, quote, observed_at, "expired", extra={"reason": reason})

    def _emit(
        self,
        state: MomentumRetestState,
        quote: Mapping[str, Any],
        observed_at: datetime,
        event_type: str,
        *,
        assumed_fill_price: float | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> None:
        if event_type in state.event_types:
            return
        state.event_types.add(event_type)
        event_key = (
            f"{ROUTE_ID}:{self.policy.version}:{state.trade_date.isoformat()}:"
            f"{state.code}:{event_type}"
        )
        snapshot = {
            "as_of_at": observed_at.isoformat(),
            "analysis_trade_date": state.trade_date.isoformat(),
            "point_in_time_only": True,
            "coverage_policy": "explicit_consumer_loss_v1",
            "liquidity_contract": MOMENTUM_LIQUIDITY_CONTRACT_VERSION,
            "route_id": ROUTE_ID,
            "route_version": self.policy.version,
            "rule_snapshot": self.policy.snapshot(),
            "quote": {
                key: (None if isinstance(quote.get(key), float) and not math.isfinite(quote[key])
                      else quote.get(key))
                for key in (
                    "code", "name", "price", "prev_close", "change_pct", "high", "low",
                    "limit_up", "amount", "volume_ratio", "avg_price", "ask1_price",
                    "ask1_volume", "bid1_price", "bid1_volume", "orderbook_imbalance",
                    "support_strength_score", "withdrawal_ratio", "source_quote_at",
                    "received_at", "updated_at",
                )
            },
            "state": state.snapshot(),
            "extra": dict(extra or {}),
        }
        self._pending[event_key] = {
            "event_key": event_key,
            "route_id": ROUTE_ID,
            "route_version": self.policy.version,
            "trade_date": state.trade_date,
            "observed_at": observed_at,
            "code": state.code,
            "name": state.name[:20],
            "event_type": event_type,
            "status": state.stage,
            "price": _safe_float(quote.get("price")),
            "assumed_fill_price": assumed_fill_price,
            "change_pct": _safe_float(quote.get("change_pct")),
            "snapshot_json": json.dumps(snapshot, ensure_ascii=False, default=str),
            "created_at": datetime.now(),
        }


shadow_engine = MomentumRetestShadowEngine()
_allowed_codes_date: date | None = None
_allowed_codes: set[str] = set()
_hydrated_date: date | None = None


async def _load_allowed_codes(db: AsyncSession, trade_date: date) -> set[str]:
    global _allowed_codes_date, _allowed_codes
    if _allowed_codes_date == trade_date:
        return set(_allowed_codes)
    result = await db.execute(
        select(StockTag.code).where(
            StockTag.board_tag == "tradeable",
            StockTag.is_st.is_(False),
            StockTag.is_suspended.is_(False),
            StockTag.is_delisting.is_(False),
            StockTag.is_ipo_recent.is_(False),
        )
    )
    blacklist_result = await db.execute(
        select(StockBlacklist.code).where(
            StockBlacklist.start_date <= trade_date,
            or_(
                StockBlacklist.end_date.is_(None),
                StockBlacklist.end_date >= trade_date,
            ),
        )
    )
    blocked_codes = {str(code) for code in blacklist_result.scalars().all()}
    _allowed_codes = {
        str(code)
        for code in result.scalars().all()
        if str(code) not in blocked_codes
    }
    _allowed_codes_date = trade_date
    return set(_allowed_codes)


async def _hydrate_engine(db: AsyncSession, trade_date: date) -> None:
    global _hydrated_date
    if _hydrated_date == trade_date:
        return
    result = await db.execute(
        select(PaperShadowEvent)
        .where(
            PaperShadowEvent.route_id == ROUTE_ID,
            PaperShadowEvent.route_version == shadow_engine.policy.version,
            PaperShadowEvent.trade_date == trade_date,
        )
        .order_by(PaperShadowEvent.observed_at, PaperShadowEvent.id)
    )
    shadow_engine.restore(result.scalars().all())
    _hydrated_date = trade_date


async def _persist_shadow_events(
    db: AsyncSession,
    pending: list[dict[str, Any]],
) -> None:
    """按 SQLite 安全变量预算追加不可变事件。"""
    for offset in range(0, len(pending), SHADOW_EVENT_INSERT_BATCH_SIZE):
        batch = pending[offset:offset + SHADOW_EVENT_INSERT_BATCH_SIZE]
        statement = sqlite_insert(PaperShadowEvent).values(batch)
        statement = statement.on_conflict_do_nothing(index_elements=["event_key"])
        await db.execute(statement)
    await db.commit()


async def scan_momentum_retest_shadow(
    db: AsyncSession,
    quotes: Iterable[Mapping[str, Any]],
    observed_at: datetime,
    *,
    coverage_loss: Mapping[str, Any] | None = None,
) -> dict[str, int]:
    """处理一轮全市场快照并追加事件；不会生成或执行任何订单。"""
    if not settings.PAPER_MOMENTUM_RETEST_SHADOW_ENABLED:
        return {"events": 0, "confirmed": 0}
    await _hydrate_engine(db, observed_at.date())
    allowed_codes = await _load_allowed_codes(db, observed_at.date())
    pending = shadow_engine.observe_batch(
        quotes, observed_at, allowed_codes, coverage_loss=coverage_loss,
    )
    if not pending:
        return {"events": 0, "confirmed": 0}
    await _persist_shadow_events(db, pending)
    shadow_engine.ack(item["event_key"] for item in pending)
    confirmed = sum(1 for item in pending if item["event_type"] == "confirmed")
    return {"events": len(pending), "confirmed": confirmed}


async def settle_momentum_retest_shadow(
    db: AsyncSession,
    as_of_date: date | None = None,
    limit: int = 500,
) -> dict[str, int]:
    """追加1/3/5/10日结算；信号日盘中后续路径因无分钟档案而明确不计。"""
    as_of_date = as_of_date or date.today()
    result = await db.execute(
        select(PaperShadowEvent)
        .where(
            PaperShadowEvent.route_id == ROUTE_ID,
            PaperShadowEvent.event_type == "confirmed",
            PaperShadowEvent.trade_date < as_of_date,
        )
        .order_by(PaperShadowEvent.trade_date, PaperShadowEvent.observed_at)
        .limit(max(int(limit), 1))
    )
    signals = result.scalars().all()
    if not signals:
        return {"signals": 0, "evaluations_added": 0}

    signal_keys = [item.event_key for item in signals]
    existing_result = await db.execute(
        select(
            PaperShadowEvaluation.signal_event_key,
            PaperShadowEvaluation.horizon_days,
        ).where(PaperShadowEvaluation.signal_event_key.in_(signal_keys))
    )
    existing = {(str(key), int(horizon)) for key, horizon in existing_result.all()}
    benchmark_cache: dict[date, float] = {}

    async def market_return(trade_day: date) -> float:
        if trade_day not in benchmark_cache:
            value = await db.scalar(
                select(func.avg(StockKline.change_pct)).where(
                    StockKline.trade_date == trade_day,
                    StockKline.change_pct.is_not(None),
                    StockKline.change_pct >= -21.0,
                    StockKline.change_pct <= 21.0,
                )
            )
            benchmark_cache[trade_day] = _safe_float(value)
        return benchmark_cache[trade_day]

    rows: list[dict[str, Any]] = []
    buy_commission = max(_safe_float(settings.PAPER_COMMISSION_RATE), 0.0)
    sell_commission = buy_commission
    stamp_tax = max(_safe_float(settings.PAPER_STAMP_TAX_RATE), 0.0)
    sell_slippage = max(_safe_float(settings.PAPER_EXECUTION_SLIPPAGE_PCT), 0.0) / 100.0
    for signal in signals:
        bars_result = await db.execute(
            select(StockKline)
            .where(
                StockKline.code == signal.code,
                StockKline.trade_date > signal.trade_date,
                StockKline.trade_date <= as_of_date,
            )
            .order_by(StockKline.trade_date)
            .limit(10)
        )
        bars = bars_result.scalars().all()
        signal_price = _safe_float(signal.assumed_fill_price)
        if signal_price <= 0 or not bars:
            continue
        for horizon in (1, 3, 5, 10):
            if len(bars) < horizon or (signal.event_key, horizon) in existing:
                continue
            observed = bars[:horizon]
            exit_close = _safe_float(observed[-1].close)
            if exit_close <= 0:
                continue
            exit_fill = exit_close * (1.0 - sell_slippage)
            gross_return = (exit_close / signal_price - 1.0) * 100.0
            buy_cash = signal_price * (1.0 + buy_commission)
            sell_cash = exit_fill * (1.0 - sell_commission - stamp_tax)
            net_return = (sell_cash / buy_cash - 1.0) * 100.0
            benchmark_nav = 1.0
            for bar in observed:
                benchmark_nav *= 1.0 + await market_return(bar.trade_date) / 100.0
            benchmark_return = (benchmark_nav - 1.0) * 100.0
            highs = [_safe_float(bar.high) for bar in observed if _safe_float(bar.high) > 0]
            lows = [_safe_float(bar.low) for bar in observed if _safe_float(bar.low) > 0]
            mfe = (max(highs) / signal_price - 1.0) * 100.0 if highs else 0.0
            mae = (min(lows) / signal_price - 1.0) * 100.0 if lows else 0.0
            rows.append({
                "signal_event_key": signal.event_key,
                "route_id": ROUTE_ID,
                "route_version": signal.route_version,
                "code": signal.code,
                "signal_trade_date": signal.trade_date,
                "signal_time": signal.observed_at,
                "horizon_days": horizon,
                "exit_trade_date": observed[-1].trade_date,
                "signal_price": signal_price,
                "exit_price": _round(exit_fill),
                "gross_return_pct": _round(gross_return),
                "net_return_pct": _round(net_return),
                "benchmark_return_pct": _round(benchmark_return),
                "excess_return_pct": _round(net_return - benchmark_return),
                "max_favorable_pct": _round(mfe),
                "max_adverse_pct": _round(mae),
                "is_positive": bool(net_return > 0 and net_return - benchmark_return > 0),
                "details_json": json.dumps({
                    "as_of_date": as_of_date.isoformat(),
                    "entry_basis": "signal_time_ask1_plus_buy_slippage",
                    "exit_basis": "horizon_close_minus_sell_slippage",
                    "benchmark_basis": "next_session_all_stock_equal_weight_close_to_close_proxy",
                    "signal_day_post_entry_path_available": False,
                    "commission_rate": buy_commission,
                    "stamp_tax_rate": stamp_tax,
                    "slippage_pct_each_side": _safe_float(settings.PAPER_EXECUTION_SLIPPAGE_PCT),
                }, ensure_ascii=False),
                "created_at": datetime.now(),
            })
    if rows:
        statement = sqlite_insert(PaperShadowEvaluation).values(rows)
        statement = statement.on_conflict_do_nothing(
            index_elements=["signal_event_key", "horizon_days"]
        )
        await db.execute(statement)
        await db.commit()
    return {"signals": len(signals), "evaluations_added": len(rows)}


def _mean(values: Iterable[Any]) -> float | None:
    cleaned = [_safe_float(value) for value in values if value is not None]
    return sum(cleaned) / len(cleaned) if cleaned else None


def build_evidence_summary(
    evaluations: Iterable[PaperShadowEvaluation],
) -> dict[str, Any]:
    rows = sorted(
        list(evaluations),
        key=lambda item: (item.signal_time, item.signal_event_key),
    )
    sample_count = len(rows)
    session_counts = Counter(item.signal_trade_date for item in rows)
    code_counts = Counter(str(item.code) for item in rows)
    sessions = len(session_counts)
    net_returns = [_safe_float(item.net_return_pct) for item in rows]
    excess_returns = [_safe_float(item.excess_return_pct) for item in rows]
    adverse_returns = [
        _safe_float(item.max_adverse_pct)
        for item in rows
        if item.max_adverse_pct is not None
    ]
    split = sample_count // 2
    first_half = net_returns[:split]
    second_half = net_returns[split:]
    remove_count = min(5, max(sample_count - 1, 0))
    without_top = sorted(net_returns)[:-remove_count] if remove_count else list(net_returns)
    avg_net = _mean(net_returns)
    avg_excess = _mean(excess_returns)
    first_half_net = _mean(first_half)
    second_half_net = _mean(second_half)
    without_top_net = _mean(without_top)
    win_rate = (
        sum(1 for value in net_returns if value > 0) / sample_count
        if sample_count
        else None
    )
    avg_adverse = _mean(adverse_returns)
    worst_adverse = min(adverse_returns, default=None)
    max_session_share = (
        max(session_counts.values(), default=0) / sample_count
        if sample_count
        else None
    )
    max_code_share = (
        max(code_counts.values(), default=0) / sample_count
        if sample_count
        else None
    )
    min_sessions = max(int(settings.PAPER_MOMENTUM_RETEST_EVAL_MIN_SESSIONS), 1)
    min_samples = max(int(settings.PAPER_MOMENTUM_RETEST_EVAL_MIN_SAMPLES), 1)
    min_avg_mae = _safe_float(settings.PAPER_MOMENTUM_RETEST_EVAL_MIN_AVG_MAE_PCT, -3.0)
    min_worst_mae = _safe_float(settings.PAPER_MOMENTUM_RETEST_EVAL_MIN_WORST_MAE_PCT, -8.0)
    max_session_share_limit = min(
        max(_safe_float(settings.PAPER_MOMENTUM_RETEST_EVAL_MAX_SESSION_SHARE, 0.15), 0.0),
        1.0,
    )
    max_code_share_limit = min(
        max(_safe_float(settings.PAPER_MOMENTUM_RETEST_EVAL_MAX_CODE_SHARE, 0.10), 0.0),
        1.0,
    )
    gates = {
        "enough_sessions": sessions >= min_sessions,
        "enough_samples": sample_count >= min_samples,
        "avg_net_return_positive": avg_net is not None and avg_net > 0,
        "avg_excess_return_positive": avg_excess is not None and avg_excess > 0,
        "first_half_positive": first_half_net is not None and first_half_net > 0,
        "second_half_positive": second_half_net is not None and second_half_net > 0,
        "top5_removed_still_positive": without_top_net is not None and without_top_net > 0,
        "avg_mae_acceptable": avg_adverse is not None and avg_adverse >= min_avg_mae,
        "worst_mae_acceptable": worst_adverse is not None and worst_adverse >= min_worst_mae,
        "session_concentration_acceptable": (
            max_session_share is not None
            and max_session_share <= max_session_share_limit
        ),
        "code_concentration_acceptable": (
            max_code_share is not None
            and max_code_share <= max_code_share_limit
        ),
    }
    evidence_gate_passed = all(gates.values())
    return {
        "horizon_days": 3,
        "sample_count": sample_count,
        "independent_sessions": sessions,
        "minimum_required_samples": min_samples,
        "minimum_required_sessions": min_sessions,
        "win_rate": _round(win_rate, 4) if win_rate is not None else None,
        "avg_net_return_pct": _round(avg_net) if avg_net is not None else None,
        "avg_excess_return_pct": _round(avg_excess) if avg_excess is not None else None,
        "first_half_avg_net_return_pct": (
            _round(first_half_net) if first_half_net is not None else None
        ),
        "second_half_avg_net_return_pct": (
            _round(second_half_net) if second_half_net is not None else None
        ),
        "top5_removed_avg_net_return_pct": (
            _round(without_top_net) if without_top_net is not None else None
        ),
        "avg_max_adverse_pct": _round(avg_adverse) if avg_adverse is not None else None,
        "worst_max_adverse_pct": _round(worst_adverse) if worst_adverse is not None else None,
        "max_session_share": (
            _round(max_session_share, 4) if max_session_share is not None else None
        ),
        "max_code_share": _round(max_code_share, 4) if max_code_share is not None else None,
        "risk_and_concentration_limits": {
            "min_avg_mae_pct": min_avg_mae,
            "min_worst_mae_pct": min_worst_mae,
            "max_session_share": max_session_share_limit,
            "max_code_share": max_code_share_limit,
        },
        "gates": gates,
        "evidence_gate_passed": evidence_gate_passed,
        "execution_enabled": False,
        "promotion_requires_manual_review": True,
    }


async def get_momentum_retest_shadow_dashboard(
    db: AsyncSession,
    trade_date: date | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    policy = shadow_engine.policy
    event_filters = [
        PaperShadowEvent.route_id == ROUTE_ID,
        PaperShadowEvent.route_version == policy.version,
    ]
    if trade_date is not None:
        event_filters.append(PaperShadowEvent.trade_date == trade_date)
    count_result = await db.execute(
        select(PaperShadowEvent.event_type, func.count(PaperShadowEvent.id))
        .where(*event_filters)
        .group_by(PaperShadowEvent.event_type)
    )
    counts = {str(event_type): int(count) for event_type, count in count_result.all()}
    events_result = await db.execute(
        select(PaperShadowEvent)
        .where(*event_filters)
        .order_by(PaperShadowEvent.observed_at.desc(), PaperShadowEvent.id.desc())
        .limit(min(max(int(limit), 1), 500))
    )
    events = events_result.scalars().all()
    evaluations_result = await db.execute(
        select(PaperShadowEvaluation).where(
            PaperShadowEvaluation.route_id == ROUTE_ID,
            PaperShadowEvaluation.route_version == policy.version,
            PaperShadowEvaluation.horizon_days == 3,
        )
    )
    evidence = build_evidence_summary(evaluations_result.scalars().all())
    return {
        "route_id": ROUTE_ID,
        "route_version": policy.version,
        "mode": "shadow_only",
        "automatic_order_connected": False,
        "requested_trade_date": trade_date.isoformat() if trade_date else None,
        "as_of_at": datetime.now().isoformat(),
        "rule_snapshot": policy.snapshot(),
        "event_counts": counts,
        "events": [
            {
                "event_key": item.event_key,
                "trade_date": item.trade_date.isoformat(),
                "observed_at": item.observed_at.isoformat(),
                "code": item.code,
                "name": item.name,
                "event_type": item.event_type,
                "status": item.status,
                "price": item.price,
                "assumed_fill_price": item.assumed_fill_price,
                "change_pct": item.change_pct,
                "snapshot": _json_loads(item.snapshot_json),
            }
            for item in events
        ],
        "forward_evidence": evidence,
        "historical_validation": {
            "intraday_point_in_time_archive_available": False,
            "daily_ohlc_proxy_allowed_for_promotion": False,
            "status": "forward_shadow_required",
            "reason": (
                "现有历史日K只有OHLC，无法知道日内高低点先后、是否为首次回踩，"
                "也无法重建确认时卖一与盘口；不得把日K形态冒充该路线的历史成交证据。"
            ),
        },
    }
