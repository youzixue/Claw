"""按真实首次买入成交追溯冻结的退出参数，不把今天的配置写回旧持仓。"""
import json
import math
from datetime import datetime

from sqlalchemy import select

from app.models.paper import PaperTradeLog
from app.models.trading import TradeFill, TradeOrder


def _object(raw) -> dict:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


async def position_exit_policy(db, *, account_name: str, position, defaults: dict, as_of: datetime):
    """返回参数和来源审计；缺失旧证据不编造、不强平，也不停止正常风险退出。"""
    fallback = dict(defaults)
    trace = {
        "basis": "legacy_or_missing_entry_evidence",
        "position_strategy_version": str(position.strategy_version or ""),
        "missing_keys": sorted(defaults),
    }
    first_buy = await db.scalar(select(PaperTradeLog).where(
        PaperTradeLog.account_id == position.account_id,
        PaperTradeLog.code == position.code,
        PaperTradeLog.trade_type == "buy",
        PaperTradeLog.trade_time >= position.buy_time,
        PaperTradeLog.trade_time <= as_of,
    ).order_by(PaperTradeLog.trade_time, PaperTradeLog.id).limit(1))
    if first_buy is None:
        return fallback, trace
    order = await db.scalar(select(TradeOrder).join(
        TradeFill, TradeFill.order_id == TradeOrder.order_id,
    ).where(
        TradeOrder.broker == "paper", TradeFill.broker == "paper",
        TradeOrder.account_id == account_name, TradeOrder.code == position.code,
        TradeOrder.side == "buy", TradeFill.side == "buy",
        TradeFill.broker_trade_id == str(first_buy.id),
        TradeFill.filled_at <= as_of, TradeOrder.created_at <= as_of,
    ).order_by(TradeFill.filled_at, TradeFill.id).limit(1))
    if order is None:
        return fallback, trace
    evidence = _object(order.risk_json).get("experiment_entry")
    if not isinstance(evidence, dict):
        return fallback, trace
    version = str(position.strategy_version or "")
    if not version or any(str(item or "") != version for item in (
        first_buy.strategy_version, order.strategy_version, evidence.get("strategy_version"),
    )):
        trace["basis"] = "entry_version_mismatch"
        return fallback, trace
    try:
        observed = datetime.fromisoformat(str(evidence.get("observed_at") or ""))
        if observed > first_buy.trade_time or observed > as_of:
            raise ValueError("future entry evidence")
    except (ValueError, TypeError):
        trace["basis"] = "entry_clock_invalid"
        return fallback, trace
    frozen = evidence.get("exit_parameters")
    if not isinstance(frozen, dict):
        return fallback, trace
    owned = {}
    for key in defaults:
        value = frozen.get(key)
        if key == "open_noise_end":
            try:
                datetime.strptime(value, "%H:%M")
            except (ValueError, TypeError):
                continue
        elif isinstance(defaults[key], bool):
            if not isinstance(value, bool):
                continue
        elif not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
            continue
        owned[key] = value
    if not owned:
        return fallback, trace
    missing = sorted(set(defaults) - set(owned))
    return {**fallback, **owned}, {
        "basis": "partial_legacy_snapshot" if missing else "frozen_entry_order",
        "order_id": order.order_id,
        "first_buy_trade_id": first_buy.id,
        "position_strategy_version": version,
        "observed_at": observed.isoformat(),
        "missing_keys": missing,
    }
