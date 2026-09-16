"""持仓退出审计：日内形态与建仓后采样峰值分离，不改变冻结退出规则。

复用追加式审计表保存新极值，按账户/仓位轮次/版本隔离，run_id索引支持重启恢复。
旧仓没有既有记录时只从今后真实报价开始，不回填历史、不把日高当作持仓峰值。
"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime
from typing import Any

from sqlalchemy import select

from app.models.paper import PaperAutoTradeLog

SCHEMA = "position_extrema_v1"


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        n = float(value)
        return n if math.isfinite(n) and n > 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def finite_profit_pct(price: Any, cost: Any) -> float | None:
    """Finite prices do not guarantee a finite price/cost percentage."""
    price, cost = _number(price), _number(cost)
    if price is None or cost is None:
        return None
    value = (price / cost - 1) * 100
    return value if math.isfinite(value) else None


def _clock(value: Any) -> datetime | None:
    try:
        at = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
        # 项目行情和成交采用北京时间naive；混入aware不静默错配时区。
        return at if at.tzinfo is None else None
    except (TypeError, ValueError):
        return None


def position_identity(position: Any) -> dict:
    return {
        "position_id": position.id,
        "account_id": position.account_id,
        "code": position.code,
        "buy_time": position.buy_time.isoformat(),
        "strategy_version": str(position.strategy_version or "legacy_unversioned"),
    }


def advance_extrema(previous: dict | None, *, position: Any, spot: Any,
                    observed_at: datetime, quote_ok: bool, max_age_sec: float) -> tuple[dict, bool]:
    """纯函数；只接收买入后、源<=接收<=决策、鲜度合格的真实现价，不接收day high。"""
    identity = position_identity(position)
    state = dict(previous or {})
    if state.get("schema") != SCHEMA or any(state.get(k) != v for k, v in identity.items()):
        state = {}
    last = _clock(state.get("last_observed_at"))
    if last and last > observed_at:
        state = {}  # 不允许未来恢复记录倒灌当前轮次。
    if not state:
        state = {
            "schema": SCHEMA, **identity,
            "coverage": "forward_observed_only_not_full_ticks",
            "history_before_first_observation": "unknown_not_backfilled",
            "first_source_at": None, "last_source_at": None, "last_observed_at": None,
            "post_entry_high": None, "post_entry_high_source_at": None,
            "post_entry_low": None, "post_entry_low_source_at": None,
            "observed_max_profit_pct": None,
            "extrema_update_count": 0,
        }
    source_at = _clock(getattr(spot, "source_quote_at", None))
    received_at = _clock(getattr(spot, "received_at", None))
    buy_at = _clock(position.buy_time)
    price = _number(getattr(spot, "price", None))
    cost = _number(position.buy_price)
    quote_round = str(getattr(spot, "quote_round_id", "") or "")
    source_last = _clock(state.get("last_source_at"))
    valid = bool(
        quote_ok and price and cost and buy_at and source_at and received_at and quote_round
        and buy_at <= source_at <= received_at <= observed_at
        and 0 <= (observed_at - source_at).total_seconds() <= max_age_sec
        and (source_last is None or source_at > source_last)
    )
    profit = finite_profit_pct(price, cost) if valid else None
    if profit is None:
        return state, False  # Do not poison extrema or fail strict JSON persistence.
    changed = False
    high = _number(state.get("post_entry_high"))
    low = _number(state.get("post_entry_low"))
    if high is None or price > high:
        state.update(post_entry_high=price, post_entry_high_source_at=source_at.isoformat())
        changed = True
    if low is None or price < low:
        state.update(post_entry_low=price, post_entry_low_source_at=source_at.isoformat())
        changed = True
    profit = round(profit, 6)
    # 使用每次观测时的实际加权成本；不能用加仓后的成本重写早先浮盈。
    old_profit = state.get("observed_max_profit_pct")
    if old_profit is None or profit > old_profit:
        state["observed_max_profit_pct"] = profit
        state["max_profit_cost_basis"] = cost
        state["max_profit_source_at"] = source_at.isoformat()
        changed = True
    if state.get("first_source_at") is None:
        state["first_source_at"] = source_at.isoformat()
    if changed:
        state["last_source_at"] = source_at.isoformat()
        state["last_observed_at"] = observed_at.isoformat()
        state["last_quote_round_id"] = quote_round
        state["extrema_update_count"] = int(state.get("extrema_update_count") or 0) + 1
    return state, changed


async def observe_position_extrema(db, *, position: Any, spot: Any, observed_at: datetime,
                                   quote_ok: bool, max_age_sec: float, persist: bool) -> dict:
    identity = position_identity(position)
    token = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:32]
    run_id = "px-" + token
    raw = await db.scalar(select(PaperAutoTradeLog.candidate_json).where(
        PaperAutoTradeLog.run_id == run_id,
        PaperAutoTradeLog.account_id == position.account_id,
        PaperAutoTradeLog.code == position.code,
        PaperAutoTradeLog.stage_code == "position_extrema",
        PaperAutoTradeLog.created_at <= observed_at,
    ).order_by(PaperAutoTradeLog.id.desc()).limit(1))
    try:
        previous = json.loads(raw or "{}")
        if not isinstance(previous, dict):
            previous = {}
    except (ValueError, TypeError):
        previous = {}
    state, changed = advance_extrema(previous, position=position, spot=spot,
                                    observed_at=observed_at, quote_ok=quote_ok,
                                    max_age_sec=max_age_sec)
    if changed and persist:
        db.add(PaperAutoTradeLog(
            account_id=position.account_id, run_id=run_id,
            trade_date=observed_at.date(), created_at=observed_at,
            trigger="position_observation", source="position-audit",
            code=position.code, name=position.name or "",
            action="position_sample", decision="observed",
            reason="记录建仓后真实报价新极值，仅审计，不生成买卖指令",
            price=float(spot.price), strategy_version=identity["strategy_version"],
            quote_round_id=str(spot.quote_round_id), as_of_at=_clock(spot.source_quote_at),
            stage_code="position_extrema", reason_code="post_entry_extrema_observed",
            candidate_json=json.dumps(state, ensure_ascii=False, allow_nan=False),
        ))
        await db.flush()
    return state


def attach_exit_audit(ctx: dict, *, position: Any, extrema: dict, quote_ok: bool) -> None:
    """顶层原字段继续兼容旧规则；新字段明确每种高点的语义及可观测边界。"""
    ctx["session_high"] = ctx.get("high")
    ctx["session_high_scope"] = "current_session_may_precede_entry"
    ctx["position_extrema"] = extrema
    ctx["post_entry_high"] = extrema.get("post_entry_high")
    cost = _number(position.buy_price)
    high = _number(ctx.get("session_high"))
    session_profit = finite_profit_pct(high, cost)
    ctx["session_high_vs_cost_pct"] = round(session_profit, 6) if session_profit is not None else None
    ctx["exit_high_basis"] = "session_high_frozen_rule_unchanged"
    ctx["exit_evaluation_quote_valid"] = bool(quote_ok)
    ctx["exit_trigger_reason"] = ""
    ctx["execution_block_code"] = ""
    ctx["execution_block_reason"] = ""
    ctx["exit_execution_status"] = "not_requested"


def block_exit(ctx: dict, *, code: str, reason: str, status: str = "blocked") -> None:
    """只增补执行状态，绝不覆盖exit_trigger_reason。"""
    ctx["execution_block_code"] = code
    ctx["execution_block_reason"] = reason
    ctx["exit_execution_status"] = status
