"""交易执行服务 — 风控前置、委托、成交回报、账户同步"""

import hashlib
import json
import math
import uuid
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from typing import Any, Optional

from fastapi import HTTPException
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError, OperationalError

from app.config.settings import settings
from app.paper.experiment import experiment_active, freeze_entry_evidence, sentiment_is_required, sentiment_quality_at, LIMIT_QUEUE_EVIDENCE_CONTRACT_VERSION
from app.models.stock import StockSpot
from app.core.stock_tagger import stock_tagger
from app.models.trading import BrokerSyncSnapshot, TradeFill, TradeOrder
from app.risk.circuit_breaker import sentiment_circuit_breaker
from app.risk.engine import RiskContext, risk_engine
from app.trading.broker import BrokerOrderRequest, get_broker_adapter
from app.trading.paper_depth_capacity import account_round_depth_evidence, visible_depth_capacity
from app.trading.paper_execution_integrity import account_execution_integrity_evidence
from app.trading.paper_authorization import (
    _paper_order_transaction, PAPER_ORDER_CHECKPOINT_FIELDS,
    mark_paper_execution_uncertain, paper_transaction_active,
)


@dataclass
class SubmitOrderCommand:
    code: str
    side: str
    price: float
    quantity: int
    broker: str = "paper"
    account_id: str = "default"
    order_type: str = "limit"
    strategy_id: str = ""
    strategy_version: str = ""
    signal_id: str = ""
    source: str = ""
    reason: str = ""
    execute: bool = True
    is_drawdown_recovery_probe: bool = False
    drawdown_recovery_limit_pct: float = 0
    entry_sector_code: str | None = None
    entry_sector_name: str | None = None
    queue_if_limit_up: bool = False
    queue_metadata: dict[str, Any] | None = None
    decision_round_id: str = ""
    decision_at: datetime | None = None
    as_of_at: datetime | None = None
    idempotency_key: str = ""
    defer_until_next_round: bool = False
    deferred_metadata: dict[str, Any] | None = None
    config_version: str = ""
    code_version: str = ""
    stop_loss_price: float | None = None
    # Compatibility input only: every immediate paper fill is checked, even False.
    # Deferred/queue paths retain their separate, non-immediate matching contracts.
    require_immediate_quote: bool = True
    # Explicit public/manual intent only; no scheduler/strategy implicitly opts in.
    after_hours_manual_intent: bool = False


def _paper_only_guard_reason(cmd: SubmitOrderCommand) -> str:
    """Return a hard-boundary reason when a simulation identity targets live broker."""

    if str(cmd.broker or "").strip().lower() == "paper":
        return ""
    # Runtime import avoids coupling the generic trading service to API module
    # initialization while keeping the account roster single-sourced.
    from app.api.v1 import paper

    account_name = str(cmd.account_id or "").strip()
    from app.paper.portfolio_contract import PORTFOLIO_ACCOUNT
    reserved_accounts = {
        *paper.PAPER_ALL_ACCOUNTS,
        *paper.PAPER_CHALLENGER_ACCOUNTS,
        PORTFOLIO_ACCOUNT,
    }
    simulation_strategy = str(cmd.strategy_id or "").strip().lower().startswith(
        "paper-"
    )
    simulation_source = str(cmd.source or "").strip() in {
        *paper.PAPER_CHALLENGER_ACCOUNT_BY_ROUTE,
        "underwater_reversal",
        "promotion_prediction",
        "tenbagger_midline",
        "reversal_pullback",
    }
    if account_name in reserved_accounts or simulation_strategy or simulation_source:
        return (
            "A-F Champion及Challenger均为纯模拟账户，禁止路由到非paper券商；"
            "真实账户必须使用独立账户标识和显式实盘策略"
        )
    return ""


def _json_dumps(value: Any) -> str:
    return json.dumps(value or {}, ensure_ascii=False, default=str)


def _json_loads_dict(value: Any) -> dict:
    try:
        parsed = json.loads(value or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _effective_risk_level(result: Any) -> str:
    """提交和两种延迟撮合共享失败关闭的汇总边界；缺结论不是pass。"""
    if not isinstance(result, dict):
        return "block"
    level = result.get("final_level")
    if (
        not isinstance(level, str)
        or level not in {"pass", "warn", "block"}
        or (result.get("evaluation_status") is not None and result.get("evaluation_status") != "complete")
        or result.get("block_reasons")
    ):
        return "block"
    return level


def _to_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError, OverflowError):
        return None


def validated_order_price(value: Any) -> float:
    """Shared API/internal boundary; preserve numeric strings, reject fake prices."""
    price = _to_float(value)
    if price is None or price <= 0:
        raise ValueError("价格必须为有限正数，不能为布尔值")
    return price


def validate_order_notional(price: float, quantity: int) -> None:
    """Representation bounds only; original lot/risk rules stay at their owners."""
    if (type(quantity) is not int or not 100 <= quantity <= 2**63 - 1
            or not math.isfinite(price * quantity)):
        raise ValueError("数量超出可存储整数范围或委托金额非有限")


def _is_sealed_limit_up_quote(spot: StockSpot | None) -> bool:
    if spot is None:
        return False
    price = _to_float(getattr(spot, "price", None))
    limit_up = _to_float(getattr(spot, "limit_up", None))
    ask1 = _to_float(getattr(spot, "ask1_price", None))
    bid1 = _to_float(getattr(spot, "bid1_price", None))
    if getattr(spot, "ask1_price", None) is not None and ask1 is None:
        return False  # Invalid supplied ask is not evidence of an empty ask queue.
    return bool(
        price
        and limit_up
        and round(price, 2) >= round(limit_up, 2)
        and (ask1 is None or ask1 <= 0)
        and bid1
        and round(bid1, 2) >= round(limit_up, 2)
    )


def _parse_queue_cutoff(value: Any) -> time:
    try:
        hour, minute = str(value or "14:50").split(":", 1)
        return time(int(hour), int(minute))
    except Exception:
        return time(14, 50)


def _order_payload(order: TradeOrder) -> dict:
    return {
        "id": order.id,
        "order_id": order.order_id,
        "broker": order.broker,
        "account_id": order.account_id,
        "code": order.code,
        "name": order.name,
        "side": order.side,
        "order_type": order.order_type,
        "price": order.price,
        "quantity": order.quantity,
        "filled_quantity": order.filled_quantity,
        "avg_fill_price": order.avg_fill_price,
        "status": order.status,
        "strategy_id": order.strategy_id,
        "strategy_version": order.strategy_version,
        "signal_id": order.signal_id,
        "source": order.source,
        "reason": order.reason,
        "idempotency_key": order.idempotency_key,
        "decision_round_id": order.decision_round_id,
        "last_fill_round_id": order.last_fill_round_id,
        "decision_at": order.decision_at.isoformat(sep=" ") if order.decision_at else None,
        "as_of_at": order.as_of_at.isoformat(sep=" ") if order.as_of_at else None,
        "config_version": order.config_version,
        "code_version": order.code_version,
        "risk_level": order.risk_level,
        "external_order_id": order.external_order_id,
        "error_message": order.error_message,
        "trade_date": order.trade_date.isoformat() if order.trade_date else None,
        "created_at": order.created_at.isoformat(sep=" ") if order.created_at else None,
        "updated_at": order.updated_at.isoformat(sep=" ") if order.updated_at else None,
    }


def _fill_payload(fill: TradeFill) -> dict:
    return {
        "id": fill.id,
        "fill_id": fill.fill_id,
        "order_id": fill.order_id,
        "broker": fill.broker,
        "external_order_id": fill.external_order_id,
        "code": fill.code,
        "side": fill.side,
        "price": fill.price,
        "quantity": fill.quantity,
        "commission": fill.commission,
        "tax": fill.tax,
        "realized_pnl": fill.realized_pnl,
        "broker_trade_id": fill.broker_trade_id,
        "decision_round_id": fill.decision_round_id,
        "fill_round_id": fill.fill_round_id,
        "trade_date": fill.trade_date.isoformat() if fill.trade_date else None,
        "filled_at": fill.filled_at.isoformat(sep=" ") if fill.filled_at else None,
    }


class _PaperRiskAccountUnavailable(ValueError):
    pass


async def _paper_account_context(db: AsyncSession, account_name: str = "default", *, allow_create: bool = True) -> tuple[float, float, float, dict, float, dict]:
    from app.api.v1 import paper
    from app.models.paper import PaperAccount

    # 五策略并行: 风控基于对应策略账户的持仓/资金 (2026-08-31 修复)
    from app.paper.portfolio_reservation import reservation_active
    if paper_transaction_active(db) or reservation_active(db) or not allow_create:
        # Do not recreate a closed/deleted account or trust a pre-lock identity map.
        with db.no_autoflush:
            accounts = (await db.scalars(select(PaperAccount).where(
                PaperAccount.account_name == (account_name or "default"),
                PaperAccount.status == "active",
            ).execution_options(populate_existing=True))).all()
        if len(accounts) != 1:
            raise _PaperRiskAccountUnavailable("锁后账户不存在、已关闭或活动账户身份不唯一")
        account = accounts[0]
    else:
        account = await paper._get_or_create_account(db, account_name or "default")
    account = await paper._refresh_account(db, account)
    current_positions, position_value = await paper._position_context(db, account)
    return (
        float(account.total_assets or account.initial_capital or 0),
        float(account.current_capital or 0),
        float(position_value or 0),
        current_positions,
        abs(float(getattr(account, "current_drawdown", account.max_drawdown or 0) or 0)),
        {"id": account.id, "name": account.account_name, "status": account.status},
    )


async def _pre_trade_risk_check(db: AsyncSession, cmd: SubmitOrderCommand) -> dict:
    from app.paper.portfolio_provenance import command_origin, PortfolioIdentityError
    try:
        portfolio_origin = await command_origin(db, cmd)
    except PortfolioIdentityError as exc:
        return {"final_level": "block", "evaluation_status": "incomplete", "checked_rules": 0,
                "block_reasons": [{"rule": "paper_portfolio_origin", "message": str(exc)}]}
    policy_account = (portfolio_origin["origin_account"] if portfolio_origin
                      else str(cmd.account_id or "default"))
    from app.paper.portfolio_reservation import reservation_active
    fresh = paper_transaction_active(db) or reservation_active(db)
    stock_status = (await stock_tagger.load_status(db, cmd.code, fresh=True, at=cmd.decision_at)
                    if fresh else await stock_tagger.load_status(db, cmd.code))
    try:
        account_options = {"account_name": str(cmd.account_id or "default")}
        if cmd.order_type == "after_hours_fixed":
            account_options["allow_create"] = False
        total_assets, cash, position_value, current_positions, current_drawdown, account_status = await _paper_account_context(
            db, **account_options
        )
    except _PaperRiskAccountUnavailable as exc:
        return {"final_level": "block", "block_reasons": [{"rule": "paper_account_identity",
                "message": str(exc)}], "stock_status": stock_status,
                "evaluation_status": "incomplete", "checked_rules": 0}
    is_internal_paper_recovery = (
        cmd.broker == "paper"
        and cmd.strategy_id == "paper-auto-short"
        and cmd.is_drawdown_recovery_probe
    )
    today = (cmd.decision_at or datetime.now()).date()
    sentiment_state = (await sentiment_circuit_breaker.get_current_state(db, today, fresh=True)
                       if fresh else await sentiment_circuit_breaker.get_current_state(db, today))
    sentiment_quality_status, sentiment_quality_reason = sentiment_quality_at(
        sentiment_state, at=cmd.decision_at or datetime.now(),
        strict=experiment_active(policy_account, broker=cmd.broker, at=cmd.decision_at or datetime.now()),
    )
    if sentiment_state.trade_date != today:
        sentiment_quality_status = "stale"
        sentiment_quality_reason = (
            f"请求{today.isoformat()}，仅有"
            f"{sentiment_state.trade_date.isoformat() if sentiment_state.trade_date else '无'}情绪快照"
        )
    ctx = RiskContext(
        code=cmd.code,
        action=cmd.side,
        price=cmd.price,
        amount=cmd.quantity,
        total_assets=total_assets,
        cash=cash,
        position_value=position_value,
        current_positions=current_positions,
        max_drawdown=current_drawdown if cmd.broker == "paper" else 0,
        is_paper_experiment=experiment_active(
            policy_account, broker=cmd.broker, at=cmd.decision_at or datetime.now(),
        ),
        sentiment_required=sentiment_is_required(policy_account),
        is_drawdown_recovery_probe=is_internal_paper_recovery,
        drawdown_recovery_limit_pct=(
            cmd.drawdown_recovery_limit_pct if is_internal_paper_recovery else 0
        ),
        sentiment_cycle=str(sentiment_state.phase or "divergence"),
        sentiment_score=float(sentiment_state.score or 0),
        sentiment_quality_status=sentiment_quality_status,
        sentiment_quality_reason=sentiment_quality_reason,
        board_tag=stock_status["board_tag"],
        is_st=stock_status["is_st"],
        is_suspended=stock_status["is_suspended"],
        is_delisting=stock_status["is_delisting"],
        is_ipo_recent=stock_status["is_ipo_recent"],
    )
    result = risk_engine.check(ctx)
    result["observed_sentiment"] = {
        "phase": ctx.sentiment_cycle, "score": ctx.sentiment_score,
        "quality_status": ctx.sentiment_quality_status,
        "quality_reason": ctx.sentiment_quality_reason,
        "source_trade_date": sentiment_state.trade_date.isoformat() if sentiment_state.trade_date else None,
        "observed_at": sentiment_state.observed_at.isoformat() if getattr(sentiment_state, "observed_at", None) else None,
    }
    result["stock_status"] = stock_status
    result["account_status"] = account_status
    if portfolio_origin is not None:
        result["paper_portfolio_origin"] = portfolio_origin
        if cmd.side == "buy":
            from app.paper.portfolio_wallet import validate_command_budget
            try:
                result["paper_portfolio_budget"] = await validate_command_budget(db, cmd, portfolio_origin)
            except (PortfolioIdentityError, KeyError, ValueError, TypeError) as exc:
                result["final_level"] = "block"
                result["block_reasons"] = list(result.get("block_reasons") or []) + [{
                    "rule": "paper_portfolio_budget", "message": str(exc)}]
        if cmd.side == "buy" and _effective_risk_level(result) != "pass":
            result["final_level"] = "block"
            result["block_reasons"] = list(result.get("block_reasons") or []) + [{
                "rule": "paper_portfolio_risk", "message": "共享组合新入场必须完整通过原策略风控，警告不放行"}]
    # 身份/板块/人工封禁是成交硬边界，不能靠关闭可配置规则绕过。
    suspended_exit = cmd.side == "sell" and (
        stock_status.get("is_suspended") is True or stock_status.get("board_tag") == "suspended")
    if (cmd.side == "buy" and stock_status.get("is_tradeable") is not True) or suspended_exit:
        reason = {
            "rule": "stock_identity_boundary", "category": "blacklist",
            "message": ("证券当前停牌，禁止模拟卖出成交" if suspended_exit else
                        "证券身份未知/冲突或存在板块、停牌、ST、退市、人工限制，禁止买入"),
            "suggestion": "核实当前证券身份与风险证据；不能以规则开关代替授权",
        }
        result["final_level"] = "block"
        result["block_reasons"] = list(result.get("block_reasons") or []) + [reason]
        result["decisions"] = list(result.get("decisions") or []) + [{**reason, "level": "block"}]
    return result


async def _locked_paper_risk_evidence(db, order, cmd, execution, *, block_warn, initial_risk):
    """Reuse the real risk chain against current projections after the fill lock.

    This is not historical PIT, a quote refresh, a new signal confirmation, or
    physical-COMMIT evidence. The existing final ledger clock still runs later.
    """
    from app.api.v1 import paper
    if not paper_transaction_active(db):
        raise HTTPException(403, "锁后风控必须属于当前模拟成交事务")
    from app.trading.paper_authorization import _local_clock
    at = paper._public_order_clock()

    def invalid_clock(completed=None):
        reason = "锁后风控执行时钟缺失、非本地、回退或已跨原交易日"
        order.status, order.risk_level, order.error_message = "risk_blocked", "block", reason
        return {
            "contract_version": "paper_locked_risk_v1_20260914",
            "scope": "current_projection_under_process_fill_lock",
            "status": "blocked", "reason": reason, "reason_code": "invalid_risk_evaluation_clock",
            "order_id": order.order_id, "account_name": str(cmd.account_id or "default"),
            "code": cmd.code, "side": cmd.side, "price": execution["fill_price"],
            "quantity": execution["filled_quantity"], "quote_round_id": execution["quote_round_id"],
            "evaluated_at": at.isoformat() if isinstance(at, datetime) else None,
            "completed_at": completed.isoformat() if isinstance(completed, datetime) else None,
            "block_warn": bool(block_warn),
            "result": {"final_level": "block", "checked_rules": 0, "evaluation_status": "incomplete",
                       "block_reasons": [{"rule": "paper_risk_clock", "message": reason}]},
        }
    try:
        dispatch = _local_clock(execution["dispatch_validated_at"])
        checked_at = _local_clock(at)
        if checked_at < dispatch or checked_at.date() != dispatch.date():
            return invalid_clock()
    except (ValueError, TypeError, OverflowError):
        return invalid_clock()
    checked_cmd = replace(cmd, price=execution["fill_price"],
                          quantity=execution["filled_quantity"], decision_at=at)
    result = await _pre_trade_risk_check(db, checked_cmd)
    # Freeze only the owned risk contract, never carry mutable order/execution
    # metadata back into its own receipt (also prevents recursive diagnostic trees).
    fields = ("code", "action", "final_level", "decisions", "block_reasons", "warnings",
              "suggestions", "total_rules", "checked_rules", "evaluation_status", "evaluation_errors",
              "observed_sentiment", "stock_status", "account_status", "paper_portfolio_budget")
    result = (_json_loads_dict(_json_dumps({k: result[k] for k in fields if k in result}))
              if isinstance(result, dict) else {"final_level": "block"})
    completed = paper._public_order_clock()
    try:
        completed_at = _local_clock(completed)
        if completed_at < checked_at or completed_at.date() != checked_at.date():
            return invalid_clock(completed)
    except (ValueError, TypeError, OverflowError):
        return invalid_clock(completed)
    prior_account = initial_risk.get("account_status") or {}
    current_account = result.get("account_status") or {}
    if prior_account.get("id") is not None and prior_account.get("id") != current_account.get("id"):
        result["final_level"] = "block"
        result["block_reasons"] = list(result.get("block_reasons") or []) + [{
            "rule": "paper_account_identity", "message": "原风险账户与锁后活动账户不一致，禁止转入同名新账户"}]
    level = _effective_risk_level(result)
    blocked = level == "block" or (block_warn and level == "warn")
    messages = result.get("block_reasons") or result.get("warnings") or []
    reason = ("锁后风控复核未通过：" + ("；".join(str(m.get("message") or "")
              for m in messages if isinstance(m, dict)) or "风控结果无效或状态变化")) if blocked else ""
    proof = {
        "contract_version": "paper_locked_risk_v1_20260914",
        "scope": "current_projection_under_process_fill_lock",
        "status": "blocked" if blocked else "validated", "reason": reason,
        "order_id": order.order_id, "account_name": str(cmd.account_id or "default"),
        "code": cmd.code, "side": cmd.side, "price": checked_cmd.price, "quantity": checked_cmd.quantity,
        "quote_round_id": execution["quote_round_id"], "evaluated_at": at.isoformat(),
        "completed_at": completed_at.isoformat(), "block_warn": bool(block_warn),
        "initial_account_id": prior_account.get("id"),
        "result": result,
    }
    order.risk_level = level
    if blocked:
        order.status = "risk_blocked"
        order.error_message = reason
    return proof


async def _record_paper_account_integrity(db, order, execution, risk):
    """Freeze a read-only ledger/receipt check in the existing atomic fill unit."""
    proof = await account_execution_integrity_evidence(db, order, execution)
    execution["account_execution_integrity"] = proof
    risk["paper_account_execution_integrity"] = proof
    if proof["status"] != "validated":
        proof["previous_final_level"] = risk.get("final_level")
        risk["final_level"] = "block"
        risk["block_reasons"] = list(risk.get("block_reasons") or []) + [{
            "rule": "paper_account_execution_integrity", "message": proof["reason"]}]
        order.status, order.risk_level, order.error_message = "risk_blocked", "block", proof["reason"]
    order.risk_json = _json_dumps(risk)
    return proof["status"] == "validated"


async def _paper_execution_spot(db: AsyncSession, code: str):
    """行情轮次上下文存在时只读该轮 owned snapshot，缺失即返回 None。"""
    from app.api.v1 import paper

    return await paper._spot_by_code(db, code)


async def _order_fills(db: AsyncSession, order_id: str) -> list[TradeFill]:
    return list(
        (
            await db.scalars(
                select(TradeFill)
                .where(TradeFill.order_id == order_id)
                .order_by(TradeFill.filled_at, TradeFill.id)
            )
        ).all()
    )


def _paper_fill_request_id(
    order: TradeOrder, *, round_id: str = "", queued: bool = False,
) -> str:
    """保持既有请求键不变；同轮幂等本身不保证跨轮重启安全。"""
    if queued:
        return "lqf-" + uuid.uuid5(
            uuid.NAMESPACE_URL, f"limit-queue:{order.order_id}"
        ).hex[:24]
    return "pf-" + uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"{order.order_id}:{round_id}:{order.filled_quantity or 0}",
    ).hex[:24]


async def _block_unreconciled_paper_order(
    db: AsyncSession, order: TradeOrder, *, queued: bool = False,
) -> dict | None:
    """只拦截可精确归属本委托的缺失回报，不补写历史成交或猜测关联。

    paper 账本目前可能先于 TradeFill 提交。未完成回报的旧请求不能用新
    行情轮次继续撮合，也不能把旧成交重贴为当前时间；保留原账本待审计。
    这不是跨表原子提交或全账户恢复协议。
    """
    from app.models.paper import PaperAccount, PaperTradeLog

    trades = (await db.execute(
        select(PaperTradeLog.id, PaperTradeLog.signal_id, PaperTradeLog.fill_round_id)
        .join(PaperAccount, PaperAccount.id == PaperTradeLog.account_id)
        .where(
            PaperAccount.account_name == str(order.account_id or "default"),
            PaperTradeLog.code == order.code,
            PaperTradeLog.trade_type == order.side,
        )
        .order_by(PaperTradeLog.id)
        .execution_options(autoflush=False)
    )).all()
    for trade_id, signal_id, fill_round_id in trades:
        if not queued and not fill_round_id:
            # 无原始轮次的历史记录不能推导请求身份，保持 unknown。
            continue
        request_id = _paper_fill_request_id(
            order, round_id=str(fill_round_id or ""), queued=queued,
        )
        if signal_id not in {
            request_id, f"chlg-{request_id}", f"auto-sell-{request_id}",
            f"auto-tenbagger_midline-{request_id}",
        }:
            continue
        fills = (await db.execute(
            select(TradeFill.fill_id)
            .where(
                TradeFill.broker == "paper",
                TradeFill.order_id == order.order_id,
                TradeFill.broker_trade_id == str(trade_id),
            )
            .execution_options(autoflush=False)
        )).scalars().all()
        expected_fill_id = f"fill-{request_id}"
        if list(fills) == [expected_fill_id]:
            continue
        risk_payload = _json_loads_dict(order.risk_json)
        risk_payload["paper_execution_integrity"] = {
            "policy_version": "paper-orphan-fill-guard-v1",
            "previous_final_level": risk_payload.get("final_level"),
            "reason_code": (
                "paper_trade_without_fill" if not fills
                else "paper_trade_fill_identity_conflict"
            ),
            "paper_trade_id": trade_id,
            "expected_request_id": request_id,
            "expected_fill_id": expected_fill_id,
            "original_fill_round_id": fill_round_id,
            "automatic_replay_allowed": False,
        }
        order.status = "risk_blocked"
        order.risk_level = "block"
        order.error_message = (
            "模拟账本已有本委托成交，但成交回报缺失或身份冲突；"
            "停止该委托继续撮合，保留原成交待审计"
        )
        risk_payload["final_level"] = "block"
        prior_reasons = risk_payload.get("block_reasons")
        risk_payload["block_reasons"] = (
            list(prior_reasons) if isinstance(prior_reasons, list) else []
        ) + [order.error_message]
        order.risk_json = _json_dumps(risk_payload)
        await db.commit()
        await db.refresh(order)
        metadata_key = "paper_limit_up_queue" if queued else "paper_deferred_order"
        return {
            "event": "risk_blocked",
            "reason": order.error_message,
            "order": _order_payload(order),
            "risk": risk_payload,
            "fills": [],
            "queue" if queued else "deferred": risk_payload.get(metadata_key, {}),
        }
    return None


def _depth_fill_plan(
    spot: Any,
    *,
    side: str,
    limit_price: float,
    remaining_quantity: int,
) -> tuple[int, float | None, list[dict[str, Any]]]:
    """按五档可见深度保守计算下一轮可成交量；腾讯档位量单位为手。"""
    raw_ratio = _to_float(settings.PAPER_DEPTH_MAX_PARTICIPATION_RATIO)
    limit_price = _to_float(limit_price)
    if (raw_ratio is None or limit_price is None or limit_price <= 0
            or side not in {"buy", "sell"} or type(remaining_quantity) is not int
            or not 100 <= remaining_quantity <= 2**63 - 1):
        return 0, None, []
    ratio = max(0.0, min(1.0, raw_ratio))
    if ratio <= 0:
        return 0, None, []
    prefix = "ask" if side == "buy" else "bid"
    levels: list[dict[str, Any]] = []
    quantity_left = int(remaining_quantity)
    total_value = 0.0
    total_quantity = 0
    for level in range(1, 6):
        price = _to_float(getattr(spot, f"{prefix}{level}_price", None))
        hands = _to_float(getattr(spot, f"{prefix}{level}_volume", None))
        if price is None or price <= 0 or hands is None or hands <= 0:
            continue
        price_allowed = (
            price <= float(limit_price) + 1e-9
            if side == "buy"
            else price >= float(limit_price) - 1e-9
        )
        if not price_allowed:
            continue
        scaled_hands = hands * 100 * ratio / 100.0
        if not math.isfinite(scaled_hands):
            return 0, None, []
        available = int(math.floor(scaled_hands)) * 100
        take = min(quantity_left, available)
        take = (take // 100) * 100
        if take < 100:
            continue
        next_value = total_value + price * take
        if not math.isfinite(next_value):
            return 0, None, []
        levels.append({"level": level, "price": price, "quantity": take})
        total_value = next_value
        total_quantity += take
        quantity_left -= take
        if quantity_left < 100:
            break
    average = round(total_value / total_quantity, 4) if total_quantity else None
    return total_quantity, average, levels


async def _existing_order_result(
    db: AsyncSession,
    idempotency_key: str,
) -> dict | None:
    if not idempotency_key:
        return None
    order = await db.scalar(
        select(TradeOrder).where(TradeOrder.idempotency_key == idempotency_key)
    )
    if order is None:
        return None
    fills = await _order_fills(db, order.order_id)
    return {
        "order": _order_payload(order),
        "risk": _json_loads_dict(order.risk_json),
        "fills": [_fill_payload(row) for row in fills],
        "idempotent_replay": True,
    }


async def _dispatch_broker_order(broker, db, req, *, immediate_evidence_json=""):
    """唯一内部成交调用域；此处不替代上游风控/撮合验收。"""
    if getattr(broker, "broker_name", None) == "paper":
        from app.trading.paper_authorization import _paper_execution_scope
        with _paper_execution_scope(db, req, immediate_evidence_json=immediate_evidence_json):
            return await broker.place_order(db, req)
    return await broker.place_order(db, req)


class _BrokerDispatchFailure(Exception):
    """Only a failed broker call may become a rejected order after rollback."""
    def __init__(self, error):
        self.error = error
        super().__init__(str(error))


async def _reject_unchanged_paper_order(db, order, checkpoint, *, reason, risk_json):
    """CAS the exact pre-fill checkpoint; never overwrite another execution.

    The failed unit has already rolled back. A new owned, single-row UPDATE
    checks the original leaves atomically, even if a writer bypasses this
    process's Python lock. No ORM dirty state or historical fills are committed.
    """
    from app.api.v1 import paper
    try:
        frozen = dict(checkpoint or ())
        if (tuple(frozen) != PAPER_ORDER_CHECKPOINT_FIELDS
                or frozen["broker"] != "paper"
                or frozen["status"] not in {"pending", "submitted", "partial"}
                or type(frozen["id"]) is not int or frozen["id"] <= 0):
            raise HTTPException(409, "拒单缺少原始委托检查点，执行结果待核对")
        async with paper._TRADE_LOCK:
            try:
                if db.new or db.dirty or db.deleted:
                    raise HTTPException(409, "拒单事务存在未归属变更，执行结果待核对")
                table = TradeOrder.__table__
                statement = table.update().where(*(
                    table.c[name] == frozen[name] for name in PAPER_ORDER_CHECKPOINT_FIELDS
                )).values(status="rejected", error_message=reason, risk_json=risk_json,
                          updated_at=datetime.now())
                with db.no_autoflush:
                    result = await db.execute(statement)
                if result.rowcount != 1:
                    raise HTTPException(409, "拒单前委托已变化，禁止覆盖；执行结果待核对")
                await db.commit()
                await db.refresh(order)
            except BaseException:
                await db.rollback()
                raise
    except BaseException as exc:
        mark_paper_execution_uncertain(exc)
        # Includes cancellation/commit acknowledgement loss: never guess a result.
        try:
            await db.rollback()
        except BaseException as cleanup_error:
            mark_paper_execution_uncertain(cleanup_error)
            raise
        raise


async def _dispatch_for_service(*args, **kwargs):
    try:
        return await _dispatch_broker_order(*args, **kwargs)
    except OperationalError:
        raise
    except Exception as exc:
        raise _BrokerDispatchFailure(exc) from exc


async def submit_order(db: AsyncSession, cmd: SubmitOrderCommand) -> dict:
    from app.paper.portfolio_contract import PORTFOLIO_ACCOUNT
    if cmd.order_type == "after_hours_fixed" and cmd.account_id == PORTFOLIO_ACCOUNT:
        raise HTTPException(403, "共享组合不接受人工盘后意图，禁止先进入组合预留事务")
    if (cmd.account_id == PORTFOLIO_ACCOUNT and isinstance(cmd.side, str)
            and cmd.side.lower().strip() == "buy"):
        from app.paper.portfolio_reservation import shared_order_reservation
        async with shared_order_reservation(db):
            return await _submit_order(db, cmd)
    try:
        return await _submit_order(db, cmd)
    except IntegrityError:
        if cmd.order_type != "after_hours_fixed" or not cmd.idempotency_key:
            raise
        # A concurrent registration won the unique key. Never insert a second intent
        # or reset priority; no broker/ledger was invoked by this mode.
        await db.rollback()
        existing = await _existing_order_result(db, cmd.idempotency_key)
        if existing is None:
            raise
        _assert_order_replay_identity(existing, cmd)
        return existing


def _assert_order_replay_identity(existing, cmd):
    if any(existing["order"][key] != getattr(cmd, key) for key in (
            "code", "side", "broker", "account_id", "order_type", "price", "quantity")):
        raise HTTPException(409, "幂等键对应另一模式/身份/价量，不允许复用或重置优先级")


async def _register_paper_after_hours_intent(db, order, cmd, initial_risk):
    from app.api.v1 import paper
    from app.trading.paper_after_hours_execution import intent_evidence, clock_valid
    risk = dict(initial_risk)
    # Drop the uncommitted preflight row BEFORE waiting for the process lock.
    # Otherwise checkpoint-first would leave a naked pending order after cancellation.
    values = {column.name: getattr(order, column.name) for column in TradeOrder.__table__.columns
              if column.name != "id"}
    await db.rollback()
    # No broker dispatch: this scope protects registration/revalidation, not a fill.
    async with _paper_order_transaction(db):
        existing = await _existing_order_result(db, str(cmd.idempotency_key or ""))
        if existing is not None:
            _assert_order_replay_identity(existing, cmd)
            return existing
        accepted = paper._public_order_clock()
        if not isinstance(accepted, datetime) or accepted.tzinfo is not None:
            raise HTTPException(409, "盘后登记缺少可靠本地时钟")
        values.update(created_at=accepted, updated_at=accepted)
        order = TradeOrder(**values)
        db.add(order)
        await db.flush()
        locked_risk = await _pre_trade_risk_check(db, replace(cmd, decision_at=accepted))
        risk["paper_after_hours_locked_risk"] = locked_risk
        initial_account = initial_risk.get("account_status", {}).get("id")
        current_account = locked_risk.get("account_status", {}).get("id")
        if (_effective_risk_level(locked_risk) == "block" or initial_account is None
                or initial_account != current_account):
            order.status, order.risk_level = "risk_blocked", "block"
            order.error_message = "盘后意图锁后风控未通过；未生成任何成交"
        else:
            evidence = await intent_evidence(db, cmd, accepted_at=accepted,
                                            validated_at=paper._public_order_clock())
            # T+1 is checked at intent registration too, not only a hypothetical future fill.
            if cmd.side == "sell" and evidence["status"] == "waiting":
                from app.models.paper import PaperAccount, PaperPosition
                account = await db.scalar(select(PaperAccount).where(
                    PaperAccount.account_name == cmd.account_id, PaperAccount.status == "active"))
                position = await db.scalar(select(PaperPosition).where(
                    PaperPosition.account_id == account.id, PaperPosition.code == cmd.code,
                    PaperPosition.is_closed.is_(False))) if account else None
                available = await paper._available_sell_amount(db, position, accepted.date()) if position else 0
                evidence["sellable_quantity_at_registration"] = available
                if cmd.quantity > available:
                    evidence.update(status="rejected", reason="T_plus_1_or_insufficient_sellable_quantity")
            terminal_at = paper._public_order_clock()
            validated_at = datetime.fromisoformat(evidence["validated_at"])
            if not isinstance(terminal_at, datetime) or terminal_at.tzinfo is not None:
                raise HTTPException(409, "盘后登记终验时钟不可用")
            if (not clock_valid(terminal_at) or not cmd.decision_at <= accepted <= validated_at <= terminal_at
                    or terminal_at.date() != accepted.date() or cmd.decision_at.date() != accepted.date()):
                evidence.update(status="rejected", reason="registration_crossed_session_end_or_clock_rollback")
            evidence.update(order_id=order.order_id, acceptance_sequence=order.id,
                            requested_at=cmd.decision_at.isoformat(),
                            numeric_account_id=initial_account,
                            terminal_validated_at=terminal_at.isoformat())
            risk["paper_after_hours_intent"] = evidence
            if evidence["status"] == "rejected":
                order.status = "rejected"
                order.error_message = evidence["reason"]
            else:
                order.status = "submitted" if cmd.execute else "accepted"
                order.external_order_id = "paper-after-hours-" + order.order_id if cmd.execute else None
                order.error_message = ("等待独立对手与队列证据；非五档撮合，未冻结现金/持仓且未成交"
                                       if cmd.execute else "dry_run: 专用模式意图校验，未登记可撮合委托")
        order.risk_json = _json_dumps(risk)
    await db.refresh(order)
    return {"order": _order_payload(order), "risk": risk, "fills": []}


async def _submit_order(db: AsyncSession, cmd: SubmitOrderCommand) -> dict:
    side = cmd.side.lower().strip() if isinstance(cmd.side, str) else ""
    if side not in {"buy", "sell"}:
        raise HTTPException(status_code=400, detail="side 仅支持 buy/sell")
    # 所有后续风控/实验门禁/撮合使用同一方向；不能只规范化落库字段。
    cmd.side = side
    if cmd.order_type == "after_hours_fixed":
        from app.trading.paper_after_hours_execution import original_declaration_parameters
        try:
            original_declaration_parameters(cmd.code, side, cmd.price, cmd.quantity)
        except (TypeError, ValueError, OverflowError) as exc:
            raise HTTPException(400, "盘后原委托参数不在支持的2026申报子集内: " + str(exc)) from None
    try:
        price = validated_order_price(cmd.price)
        validate_order_notional(price, cmd.quantity)
        if cmd.quantity % 100 != 0:
            raise ValueError("invalid lot quantity")
    except (TypeError, ValueError, OverflowError):
        raise HTTPException(status_code=400, detail="价格/委托金额必须为有限正数，数量必须为可存储的100股整数倍") from None
    cmd.price = price
    if cmd.order_type not in {"limit", "market", "after_hours_fixed"}:
        raise HTTPException(400, "未知order_type，不允许退化为普通限价或盘后模式")
    if cmd.order_type == "after_hours_fixed":
        if cmd.broker != "paper" or cmd.after_hours_manual_intent is not True:
            raise HTTPException(403, "盘后专用模式仅接受显式人工paper意图，自动/实盘未授权")
        if cmd.queue_if_limit_up or cmd.defer_until_next_round or cmd.queue_metadata or cmd.deferred_metadata:
            raise HTTPException(400, "盘后模式禁止混用普通五档/涨停排队/deferred合同")
        from app.data.sources.after_hours_source import exchange_of
        from app.paper.portfolio_contract import PORTFOLIO_ACCOUNT
        from app.api.v1 import paper
        if cmd.account_id not in paper.PAPER_ALL_ACCOUNTS:
            raise HTTPException(403, "盘后人工意图仅限已有常规paper账户，候选/共享/未知身份不接受")
        from app.models.paper import PaperAccount
        with db.no_autoflush:
            existing_accounts = list((await db.scalars(select(PaperAccount.id).where(
                PaperAccount.account_name == cmd.account_id, PaperAccount.status == "active"))).all())
        if len(existing_accounts) != 1:
            raise HTTPException(403, "盘后意图必须绑定唯一已有活动账户，禁止自动创建/重开账户")
        try:
            exchange_of(cmd.code)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        # Never inherit an old Tencent round clock or let a caller backdate this session.
        cmd.decision_at = paper._public_order_clock()
        if not isinstance(cmd.decision_at, datetime) or cmd.decision_at.tzinfo is not None:
            raise HTTPException(409, "盘后人工意图缺少可靠本地请求时钟")
        cmd.as_of_at = cmd.decision_at
        cmd.decision_round_id = "after-hours-intent-" + cmd.decision_at.date().isoformat()
        cmd.strategy_version = cmd.strategy_version or paper._strategy_version(cmd.account_id)

    paper_only_reason = _paper_only_guard_reason(cmd)
    if paper_only_reason:
        raise HTTPException(status_code=403, detail=paper_only_reason)

    decision_now = cmd.decision_at
    if cmd.broker == "paper" and cmd.order_type != "after_hours_fixed":
        from app.api.v1 import paper
        from app.data.quote_round import quote_code_version, quote_config_version

        round_context = paper._quote_round_context()
        if decision_now is None and isinstance(round_context.get("committed_at"), datetime):
            decision_now = round_context["committed_at"]
        cmd.strategy_version = str(
            cmd.strategy_version
            or paper._strategy_version(str(cmd.account_id or "default"))
        )
        cmd.decision_round_id = str(
            cmd.decision_round_id or round_context.get("round_id") or ""
        )
        if cmd.as_of_at is None and isinstance(round_context.get("as_of_at"), datetime):
            cmd.as_of_at = round_context["as_of_at"]
        cmd.config_version = str(
            cmd.config_version
            or round_context.get("config_version")
            or quote_config_version()
        )
        cmd.code_version = str(
            cmd.code_version
            or round_context.get("code_version")
            or quote_code_version()
        )
    decision_now = decision_now or datetime.now()
    cmd.decision_at = decision_now

    existing = await _existing_order_result(db, str(cmd.idempotency_key or ""))
    if existing is not None:
        _assert_order_replay_identity(existing, cmd)
        return existing

    from app.paper.portfolio_contract import PORTFOLIO_ACCOUNT
    if cmd.account_id == PORTFOLIO_ACCOUNT and side == "buy":
        from app.paper.portfolio_reservation import bind_reservation_clock
        await bind_reservation_clock(db, cmd)
        decision_now = cmd.decision_at

    broker = get_broker_adapter(cmd.broker)
    order_id = f"ord-{decision_now.strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}"
    risk = await _pre_trade_risk_check(db, cmd)
    portfolio_origin = risk.get("paper_portfolio_origin")
    policy_account = portfolio_origin["origin_account"] if portfolio_origin else cmd.account_id
    if cmd.side == "buy" and (portfolio_origin or experiment_active(policy_account, broker=cmd.broker, at=decision_now)):
        # 根键保留首次下单证据；后续重验只添加fill_risk，不覆盖入场分组。
        risk["experiment_entry"] = await freeze_entry_evidence(
            db, policy_account, at=decision_now,
            sentiment=dict(risk.get("observed_sentiment") or {}),
        )
        if portfolio_origin:
            risk["experiment_entry"].update(
                strategy_version=cmd.strategy_version,
                origin_account=policy_account,
                origin_strategy_version=portfolio_origin["origin_version"],
                portfolio_version=portfolio_origin["portfolio_version"],
                exit_parameters=portfolio_origin["entry_policy"]["exit_parameters"],
                exit_mode=portfolio_origin["entry_policy"]["exit_mode"],
            )
    if cmd.strategy_version:
        risk["strategy_version"] = cmd.strategy_version
    if cmd.decision_round_id:
        risk["decision_round_id"] = cmd.decision_round_id
    risk_level = _effective_risk_level(risk)

    order = TradeOrder(
        order_id=order_id,
        broker=cmd.broker,
        account_id=cmd.account_id,
        code=cmd.code,
        side=side,
        order_type=cmd.order_type,
        price=cmd.price,
        quantity=cmd.quantity,
        status="pending",
        strategy_id=cmd.strategy_id,
        strategy_version=cmd.strategy_version or None,
        signal_id=cmd.signal_id,
        source=cmd.source,
        reason=cmd.reason,
        idempotency_key=str(cmd.idempotency_key or "") or None,
        decision_round_id=cmd.decision_round_id or None,
        decision_at=decision_now,
        as_of_at=cmd.as_of_at,
        config_version=cmd.config_version or None,
        code_version=cmd.code_version or None,
        risk_level=risk_level,
        risk_json=_json_dumps(risk),
        trade_date=decision_now.date(),
    )
    db.add(order)
    await db.flush()

    if risk_level == "block":
        order.status = "risk_blocked"
        order.error_message = "；".join(
            item.get("message", "") for item in risk.get("block_reasons", [])
        ) or "风控拦截"
        await db.commit()
        await db.refresh(order)
        return {"order": _order_payload(order), "risk": risk, "fills": []}

    if cmd.order_type == "after_hours_fixed":
        return await _register_paper_after_hours_intent(db, order, cmd, risk)

    if not cmd.execute:
        order.status = "accepted"
        order.error_message = "dry_run: 已通过风控，未发送委托"
        await db.commit()
        await db.refresh(order)
        return {"order": _order_payload(order), "risk": risk, "fills": []}

    # 涨停排队只在显式开启的 paper 买单上生效。这里仅登记委托，不能把
    # “已报到买一队列”伪造成即时成交；后续由实时成交量/开板证据撮合。
    if cmd.broker == "paper" and side == "buy" and cmd.queue_if_limit_up:
        spot = await _paper_execution_spot(db, cmd.code)
        spot_price = _to_float(getattr(spot, "price", None)) if spot else None
        spot_limit_up = _to_float(getattr(spot, "limit_up", None)) if spot else None
        spot_ask1 = _to_float(getattr(spot, "ask1_price", None)) if spot else None
        at_limit_without_offer = bool(
            spot_price
            and spot_limit_up
            and round(spot_price, 2) >= round(spot_limit_up, 2)
            and (spot_ask1 is None or spot_ask1 <= 0)
        )
        if at_limit_without_offer and not _is_sealed_limit_up_quote(spot):
            order.status = "rejected"
            order.error_message = "涨停盘口缺少有效买一队列，禁止模拟排队或即时成交"
            await db.commit()
            await db.refresh(order)
            return {"order": _order_payload(order), "risk": risk, "fills": []}
        if _is_sealed_limit_up_quote(spot):
            limit_up = float(getattr(spot, "limit_up", 0) or 0)
            if cmd.price < limit_up - 0.005:
                order.status = "rejected"
                order.error_message = "涨停排队委托价低于涨停价，无法进入买一队列"
                await db.commit()
                await db.refresh(order)
                return {"order": _order_payload(order), "risk": risk, "fills": []}

            # Unknown original queue evidence is not a zero-length queue or
            # a zero session-volume baseline. Preserve legitimate numeric zero.
            original_hands = [
                _to_float(getattr(spot, key, None)) for key in ("volume", "bid1_volume")
            ]
            if any(value is None or value < 0 or not value.is_integer()
                   for value in original_hands):
                order.status = "rejected"
                order.error_message = "queue_evidence_invalid：原排队累计量/买一数量缺失或非法，禁止补造零基线"
                await db.commit()
                await db.refresh(order)
                return {"order": _order_payload(order), "risk": risk, "fills": []}
            baseline_volume, queue_ahead = (int(value) for value in original_hands)
            queue = dict(cmd.queue_metadata or {})
            queue.update({
                "queued_at": decision_now.isoformat(sep=" "),
                "decision_round_id": cmd.decision_round_id,
                "as_of_at": cmd.as_of_at,
                "limit_up_price": limit_up,
                "queue_evidence_contract": LIMIT_QUEUE_EVIDENCE_CONTRACT_VERSION,
                "baseline_volume_hands": baseline_volume,
                "queue_ahead_hands": queue_ahead,
                "last_volume_hands": baseline_volume,
                "order_hands": max(1, (int(cmd.quantity) + 99) // 100),
                "quote_updated_at": getattr(spot, "updated_at", None),
                "strategy_version": cmd.strategy_version,
            })
            await _freeze_pending_buy_validity(db, cmd, queue, decision_at=decision_now)
            risk_payload = dict(risk)
            risk_payload["paper_limit_up_queue"] = queue
            order.name = str(getattr(spot, "name", "") or "") or None
            order.price = limit_up
            order.status = "submitted"
            order.external_order_id = f"paper-queue-{order_id}"
            order.risk_json = _json_dumps(risk_payload)
            order.error_message = (
                f"涨停价{limit_up:.2f}排队中：前方买一约"
                f"{queue['queue_ahead_hands']}手，等待开板或成交量覆盖队列"
            )
            await db.commit()
            await db.refresh(order)
            return {"order": _order_payload(order), "risk": risk_payload, "fills": []}

    if cmd.broker == "paper" and cmd.defer_until_next_round:
        if not cmd.decision_round_id:
            order.status = "rejected"
            order.error_message = "下一轮撮合缺少 decision_round_id，保守拒绝即时模拟成交"
            await db.commit()
            await db.refresh(order)
            return {"order": _order_payload(order), "risk": risk, "fills": []}
        deferred = dict(cmd.deferred_metadata or {})
        deferred.update({
            "decision_round_id": cmd.decision_round_id,
            "decision_at": decision_now,
            "as_of_at": cmd.as_of_at,
            "strategy_version": cmd.strategy_version,
            "entry_sector_code": cmd.entry_sector_code,
            "entry_sector_name": cmd.entry_sector_name,
            "stop_loss_price": cmd.stop_loss_price if cmd.stop_loss_price is not None else deferred.get("stop_loss_price"),
        })
        await _freeze_pending_buy_validity(db, cmd, deferred, decision_at=decision_now)
        risk_payload = dict(risk)
        risk_payload["paper_deferred_order"] = deferred
        order.status = "submitted"
        order.external_order_id = f"paper-deferred-{order_id}"
        order.risk_json = _json_dumps(risk_payload)
        order.error_message = "已通过风控，等待下一健康行情轮次按五档深度撮合"
        await db.commit()
        await db.refresh(order)
        return {"order": _order_payload(order), "risk": risk_payload, "fills": []}

    execution_price = cmd.price
    execution_at = decision_now
    if cmd.broker == "paper":
        from app.trading.paper_public_execution import public_fill_evidence
        execution_at = paper._public_order_clock()
        evidence = await public_fill_evidence(
            db, cmd, now=execution_at, validation_clock=paper._public_order_clock)
        risk["paper_immediate_execution"] = evidence
        # Preserve the existing response key; it no longer denotes public-only scope.
        risk["paper_public_execution"] = evidence
        order.risk_json = _json_dumps(risk)
        if evidence["status"] != "fillable":
            order.status = "rejected"
            order.error_message = evidence["reason"]
            await db.commit()
            await db.refresh(order)
            return {"order": _order_payload(order), "risk": risk, "fills": []}
        execution_price = evidence["fill_price"]
        execution_at = datetime.fromisoformat(evidence["dispatch_validated_at"])
        cmd.decision_round_id = evidence["quote_round_id"]
        # A supplied decision provenance must match, never be silently replaced.
        if cmd.as_of_at is None:
            cmd.as_of_at = datetime.fromisoformat(evidence["quote_as_of_at"])
        order.decision_round_id = cmd.decision_round_id
        order.as_of_at = cmd.as_of_at

    try:
        async with _paper_order_transaction(db, enabled=cmd.broker == "paper", order=order) as order_checkpoint:
            if cmd.broker == "paper":
                if not await _record_paper_account_integrity(db, order, evidence, risk):
                    return {"order": _order_payload(order), "risk": risk, "fills": []}
                capacity = await account_round_depth_evidence(db, order, evidence)
                evidence["account_round_depth"] = capacity
                risk["paper_account_round_depth"] = capacity
                order.risk_json = _json_dumps(risk)
                if capacity["status"] != "validated":
                    order.status = "rejected"
                    order.error_message = capacity["reason"]
                    return {"order": _order_payload(order), "risk": risk, "fills": []}
                locked_risk = await _locked_paper_risk_evidence(
                    db, order, cmd, evidence, block_warn=False, initial_risk=risk)
                evidence["locked_risk"] = locked_risk
                risk["paper_locked_risk"] = locked_risk
                order.risk_json = _json_dumps(risk)
                if locked_risk["status"] != "validated":
                    return {"order": _order_payload(order), "risk": risk, "fills": []}
                execution_json = _json_dumps(evidence)
            result = await _dispatch_for_service(
                broker, db,
                BrokerOrderRequest(
                    order_id=order_id,
                    code=cmd.code,
                    side=side,
                    price=execution_price,
                    quantity=cmd.quantity,
                    order_type=cmd.order_type,
                    reason=cmd.reason,
                    signal_id=cmd.signal_id or order_id,
                    strategy_id=cmd.strategy_id,
                    strategy_version=cmd.strategy_version,
                    source=cmd.source,
                    # 五策略并行: 成交落账到对应账户 (2026-08-31 修复)
                    account_name=str(cmd.account_id or "default"),
                    entry_sector_code=cmd.entry_sector_code,
                    entry_sector_name=cmd.entry_sector_name,
                    decision_round_id=cmd.decision_round_id,
                    fill_round_id=cmd.decision_round_id,
                    filled_at=execution_at,
                    stop_loss_price=cmd.stop_loss_price,
                ),
                immediate_evidence_json=execution_json if cmd.broker == "paper" else "",
            )
            order.external_order_id = result.external_order_id
            order.status = result.status
            order.error_message = result.error_message or None

            fill_rows = []
            if result.fills:
                if cmd.broker == "paper" and result.fills[0].raw.get("ledger_execution_timing"):
                    risk["paper_ledger_timing"] = result.fills[0].raw["ledger_execution_timing"]
                    order.risk_json = _json_dumps(risk)
                total_qty = sum(fill.quantity for fill in result.fills)
                total_value = sum(fill.price * fill.quantity for fill in result.fills)
                order.filled_quantity = total_qty
                order.avg_fill_price = round(total_value / total_qty, 4) if total_qty else None
                order.last_fill_round_id = cmd.decision_round_id or None
                for fill in result.fills:
                    row = TradeFill(
                        fill_id=fill.fill_id,
                        order_id=order.order_id,
                        broker=cmd.broker,
                        external_order_id=order.external_order_id,
                        code=cmd.code,
                        side=side,
                        price=fill.price,
                        quantity=fill.quantity,
                        commission=fill.commission,
                        tax=fill.tax,
                        realized_pnl=fill.realized_pnl,
                        broker_trade_id=fill.broker_trade_id,
                        raw_json=_json_dumps({**(fill.raw or {}),
                            "immediate_execution_evidence": json.loads(execution_json)})
                            if cmd.broker == "paper" else _json_dumps(fill.raw),
                        decision_round_id=cmd.decision_round_id or None,
                        fill_round_id=cmd.decision_round_id or None,
                        trade_date=decision_now.date(),
                        filled_at=fill.filled_at,
                    )
                    db.add(row)
                    fill_rows.append(row)

            if cmd.broker != "paper":
                await db.commit()
    except _BrokerDispatchFailure as failure:
        # The fill unit has rolled back before we write a separate rejection.
        # Receipt/flush/commit errors do NOT enter this handler: a commit may
        # have succeeded even if its acknowledgement was lost.
        exc = failure.error
        reason = str(exc.detail) if isinstance(exc, HTTPException) else str(exc)
        if isinstance(exc, HTTPException):
            risk["broker_rejection_http_status"] = exc.status_code
        if cmd.broker == "paper":
            await _reject_unchanged_paper_order(db, order, order_checkpoint,
                reason=reason, risk_json=_json_dumps(risk))
        else:
            order.status = "rejected"
            order.error_message = reason
            order.risk_json = _json_dumps(risk)
            await db.commit()
            await db.refresh(order)
        return {"order": _order_payload(order), "risk": risk, "fills": []}

    await db.refresh(order)
    for row in fill_rows:
        await db.refresh(row)
    return {
        "order": _order_payload(order),
        "risk": risk,
        "fills": [_fill_payload(row) for row in fill_rows],
    }


def _requires_pending_buy_validity(order) -> bool:
    """Only automated paper entries; never gate manual orders or position exits."""
    return (
        order.broker == "paper" and order.side == "buy"
        and order.strategy_id in {"paper-auto-short", "paper-challenger-forward", "paper-auto-t"}
    )


def _pending_buy_clock(value) -> datetime | None:
    from app.data.fund_flow_clock import local_clock

    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except (ValueError, TypeError):
            return None
    return local_clock(value)


async def _freeze_pending_buy_validity(db, cmd, metadata, *, decision_at):
    """Freeze the original confirmation, not the eventual fill or a retry clock."""
    if not _requires_pending_buy_validity(cmd):
        return
    from app.paper.account_policy import ROUTE_ACCOUNT_NAMES, challenger_execution_policy
    from app.paper.strategy_iteration_challenger import _route_shadow_version, _signal_token
    from app.models.paper import PaperShadowEvent
    from app.paper.portfolio_contract import PORTFOLIO_ACCOUNT
    if cmd.account_id == PORTFOLIO_ACCOUNT:
        from app.paper.portfolio_provenance import validated_entry_origin, PortfolioIdentityError
        try:
            origin = await validated_entry_origin(db, cmd, metadata, at=decision_at)
        except PortfolioIdentityError as exc:
            metadata["buy_validity"] = {"schema": "pending_buy_validity_v1",
                                       "status": "invalid", "reason": str(exc)}
            return
        # Reuse the exact original source validator, then bind its result to the
        # shared wallet and composite entry version. Neither clock is refreshed.
        source_cmd = replace(cmd, account_id=origin["origin_account"],
                             strategy_version=origin["origin_version"])
        metadata["candidate"] = origin["candidate"]
        metadata["confirmed_at"] = origin["confirmed_at"]
        await _freeze_pending_buy_validity(db, source_cmd, metadata, decision_at=decision_at)
        metadata["buy_validity"].update(
            account_name=cmd.account_id, strategy_version=cmd.strategy_version,
            origin_account=origin["origin_account"], origin_version=origin["origin_version"],
            portfolio_signal_key=origin["portfolio_signal_key"],
        )
        return

    candidate = metadata.get("candidate")
    candidate = candidate if isinstance(candidate, dict) else {}
    contract = {
        "schema": "pending_buy_validity_v1", "account_name": cmd.account_id,
        "code": cmd.code, "source": cmd.source, "signal_id": cmd.signal_id,
        "strategy_version": cmd.strategy_version, "status": "invalid",
    }
    metadata["buy_validity"] = contract
    route = next((route for route, account in ROUTE_ACCOUNT_NAMES.items()
                  if account == cmd.account_id), None)
    if cmd.strategy_id == "paper-auto-t":
        # Position restoration is not a challenger new-entry route.
        from app.api.v1 import paper
        evidence, reason = await paper._freeze_t_buyback_identity(db, cmd, candidate, decision_at)
        if reason:
            contract["reason"] = reason
            return
        contract["t_buyback"] = evidence
        confirmed = _pending_buy_clock(metadata.get("confirmed_at"))
        ttl = settings.PAPER_PENDING_BUY_MAX_AGE_SEC
    elif route:
        event = await db.scalar(select(PaperShadowEvent).where(
            PaperShadowEvent.event_key == str(candidate.get("event_key") or "")))
        if (
            event is None or event.event_type != "confirmed"
            or event.code != cmd.code or event.route_id != route
            or cmd.source != route or candidate.get("route_id") != route
            or candidate.get("route_version") != event.route_version
            or event.route_version != _route_shadow_version(route)
            or cmd.signal_id != _signal_token(event.event_key, route)
        ):
            contract["reason"] = "原路线confirmed事件身份缺失或不一致"
            return
        confirmed = _pending_buy_clock(event.observed_at)
        created = _pending_buy_clock(event.created_at)
        if created is None or created > decision_at:
            contract["reason"] = "原确认事件在提交时尚不可见"
            return
        ttl = challenger_execution_policy(route)["max_execution_delay_sec"]
        contract.update({
            "event_key": event.event_key, "route_id": route,
            "route_version": event.route_version,
            "event_snapshot_sha256": hashlib.sha256(
                str(event.snapshot_json).encode()).hexdigest(),
        })
    else:
        # Set by the entry loop only after its complete current confirmation.
        confirmed = _pending_buy_clock(metadata.get("confirmed_at"))
        if (candidate.get("code") != cmd.code
                or candidate.get("_source") != cmd.source):
            contract["reason"] = "原始候选代码/来源缺失或不一致"
            return
        ttl = settings.PAPER_PENDING_BUY_MAX_AGE_SEC
    if (confirmed is None or confirmed.date() != decision_at.date()
            or confirmed > decision_at):
        contract["reason"] = "原始买点确认时钟缺失、跨日或超前"
        return
    contract.update({
        "status": "valid", "confirmed_at": confirmed.isoformat(),
        "expires_at": (confirmed + timedelta(seconds=ttl)).isoformat(),
        "max_execution_delay_sec": ttl,
    })


def _pending_buy_time_reason(order, metadata, *, now) -> str:
    if not _requires_pending_buy_validity(order):
        return ""
    contract = metadata.get("buy_validity")
    if not isinstance(contract, dict) or contract.get("schema") != "pending_buy_validity_v1":
        return "legacy_buy_contract_missing：旧买单缺少冻结时效/确认合同，撤销余量，不补造证据"
    if contract.get("status") != "valid":
        return "buy_contract_invalid：" + str(contract.get("reason") or "原确认合同无效")
    if any(contract.get(key) != value for key, value in {
        "account_name": order.account_id, "code": order.code,
        "source": order.source, "signal_id": order.signal_id,
        "strategy_version": order.strategy_version,
    }.items()):
        return "buy_contract_identity_changed：原买单身份与冻结合同不一致"
    confirmed = _pending_buy_clock(contract.get("confirmed_at"))
    expires = _pending_buy_clock(contract.get("expires_at"))
    ttl = _to_float(contract.get("max_execution_delay_sec"))
    now = _pending_buy_clock(now)
    decision_at = _pending_buy_clock(order.decision_at)
    prior_evaluation = metadata.get("buy_validity_evaluation")
    prior_at = _pending_buy_clock(prior_evaluation.get("evaluated_at")) if isinstance(prior_evaluation, dict) else None
    if now is not None and ((decision_at is not None and now < decision_at)
                            or (prior_at is not None and now < prior_at)):
        return "buy_clock_regressed：撮合时钟早于原决策或上一评估，禁止倒序成交"
    if (confirmed is None or expires is None or now is None or ttl is None or ttl <= 0
            or expires <= confirmed or (expires-confirmed).total_seconds() != ttl
            or confirmed.date() != order.trade_date or now < confirmed):
        return "buy_contract_clock_invalid：原确认时钟不可验证"
    if now > expires:
        return "buy_signal_expired：超过原确认有效期，撤销未成交余量，不以当前报价续期"
    return ""


async def _pending_buy_current_status(db, order, metadata, spot, *, now):
    """Return valid / waiting / canceled using the original route, not its outcome."""
    if not _requires_pending_buy_validity(order):
        return "valid", ""
    from app.api.v1 import paper
    from app.models.paper import PaperShadowEvent
    from app.paper.strategy_iteration_challenger import (
        _route_shadow_version, _live_route_confirmation_valid, _confirmed_path_valid,
    )

    contract = metadata["buy_validity"]
    policy_account = str(order.account_id)
    from app.paper.portfolio_contract import PORTFOLIO_ACCOUNT
    if policy_account == PORTFOLIO_ACCOUNT:
        from app.paper.portfolio_provenance import validated_entry_origin, PortfolioIdentityError
        try:
            origin = await validated_entry_origin(db, order, metadata, at=now)
        except PortfolioIdentityError as exc:
            return "canceled", str(exc)
        policy_account = origin["origin_account"]
    if order.strategy_id == "paper-auto-t":
        return await paper._pending_t_buyback_confirmation(
            db, order=order, contract=contract, spot=spot, now=now)
    route = contract.get("route_id")
    if route:
        event = await db.scalar(select(PaperShadowEvent).where(
            PaperShadowEvent.event_key == contract.get("event_key")))
        if (event is None or event.event_type != "confirmed"
                or event.code != order.code or event.route_id != route
                or event.route_version != contract.get("route_version")
                or event.route_version != _route_shadow_version(route)
                or _pending_buy_clock(event.observed_at) != _pending_buy_clock(contract.get("confirmed_at"))
                or hashlib.sha256(str(event.snapshot_json).encode()).hexdigest()
                != contract.get("event_snapshot_sha256")):
            return "canceled", "buy_route_invalid：原路线版本或不可变确认事件已失效"
        audit = {}
        path_valid, path_reason = await _confirmed_path_valid(db, event, now=now, audit=audit)
        if not path_valid:
            metadata["execution_confirmation_audit"] = audit
            return "canceled", path_reason
        valid, reason = _live_route_confirmation_valid(
            event, _json_loads_dict(event.snapshot_json), spot, audit=audit)
        metadata["execution_confirmation_audit"] = audit
        return ("valid", "") if valid else (
            "waiting" if audit.get("execution_confirmation_recoverable") is True else "canceled", reason)
    return await paper._pending_primary_buy_confirmation(
        db, account_name=policy_account, source=str(order.source),
        candidate=metadata.get("candidate") or {}, spot=spot,
        limit_price=float(order.price), now=now,
    )


async def _pending_buy_outcome(db, order, risk_payload, metadata, *,
                               queued=False, status, reason, now, round_id):
    """Cancel the remainder only; pending queries then release cash/slot capacity."""
    key = "paper_limit_up_queue" if queued else "paper_deferred_order"
    response_key = "queue" if queued else "deferred"
    metadata["last_evaluated_round_id"] = round_id
    metadata["last_evaluation_reason"] = reason
    metadata["buy_validity_evaluation"] = {
        "schema": "pending_buy_evaluation_v1", "status": status,
        "evaluated_at": now.isoformat(), "quote_round_id": round_id,
        "reason": reason, "remaining_quantity": max(
            0, int(order.quantity or 0)-int(order.filled_quantity or 0)),
    }
    if status == "canceled":
        order.status = "canceled"
    order.error_message = reason
    risk_payload[key] = metadata
    order.risk_json = _json_dumps(risk_payload)
    await db.commit()
    await db.refresh(order)
    return {"event": status, "reason": reason, "order": _order_payload(order),
            "risk": risk_payload, response_key: metadata, "fills": []}


async def _pending_order_version_reason(db, order, metadata, *, now: datetime) -> str:
    """Separate a position's entry attribution from a fresh protective exit decision.

    This exception is only for an internally generated, position-bound reduction.
    It never authorizes stale buys/adds or replaces the original exit parameters.
    Quote/depth, pre-trade risk and the broker's T+1 checks still run afterwards.

    2026-09-17 复盘修复：原实现用 ``order.strategy_id == "paper-auto-short"``
    与 ``order.source == "position"`` 作为豁免前置条件，即按"订单来自哪条代码路径"
    判定，而不是按"它是不是持仓绑定的保护性减仓"判定。
    实际生产中同一个持仓 603980 在同一天产生过两个卖出订单
    （order 2283 ``paper-challenger-forward``/``momentum_first_retest`` 被拦，
    order 2284 ``paper-auto-short``/``position`` 成交），两者的
    position_id / exit_policy / exit_parameters / exit_trigger_reason / 入场版本
    逐字段相同，仅来源标签不同 —— 2284 的成交证明了 2283 的绑定证据是充分的。
    现改为只按证据判定：paper 卖单 + 入场版本存在 + 退出决策在当前版本下作出，
    其后的 position_id / exit_policy / T+1 时钟 / 可卖数量校验全部保持强制。
    """
    from app.api.v1 import paper
    from app.models.paper import PaperAccount, PaperPosition

    from app.paper.portfolio_contract import PORTFOLIO_ACCOUNT
    if order.account_id == PORTFOLIO_ACCOUNT and order.side == "buy":
        from app.paper.portfolio_provenance import validated_entry_origin, PortfolioIdentityError
        try:
            await validated_entry_origin(db, order, metadata, at=now)
        except PortfolioIdentityError as exc:
            return str(exc)
        return ""
    current_version = paper._strategy_version(str(order.account_id or "default"))
    if order.strategy_version and str(order.strategy_version) == current_version:
        return ""
    reason = (
        "延迟成交前策略版本已变化或缺失："
        f"decision={order.strategy_version or 'legacy_unversioned'}，"
        f"current={current_version}"
    )
    if not (order.broker == "paper" and order.side == "sell" and order.strategy_version):
        return reason + "；非保护性减仓订单"
    if metadata.get("exit_decision_strategy_version") != current_version:
        return reason + "；退出决策未在当前版本下作出"
    position_id = metadata.get("position_id")
    candidate = metadata.get("candidate")
    if type(position_id) is not int or position_id <= 0 or not isinstance(candidate, dict):
        return reason + "；退出缺少原持仓绑定"
    policy = candidate.get("exit_policy")
    if (not isinstance(policy, dict)
            or not isinstance(candidate.get("exit_parameters"), dict)
            or not str(candidate.get("exit_trigger_reason") or "").strip()):
        return reason + "；退出缺少原决策参数或触发证据"
    position = await db.scalar(
        select(PaperPosition).join(PaperAccount, PaperAccount.id == PaperPosition.account_id)
        .where(
            PaperPosition.id == position_id, PaperPosition.code == order.code,
            PaperPosition.is_closed.is_(False), PaperAccount.status == "active",
            PaperAccount.account_name == order.account_id,
        )
    )
    if position is None:
        return reason + "；原持仓已关闭或账户/股票不匹配"
    entry_version = str(position.strategy_version or "legacy_unversioned")
    if (str(order.strategy_version) != entry_version
            or policy.get("position_strategy_version") != str(position.strategy_version or "")):
        return reason + "；退出与原持仓入场版本不匹配"
    decision_at = _pending_buy_clock(order.decision_at)
    buy_time = _pending_buy_clock(position.buy_time)
    if (decision_at is None or buy_time is None or decision_at > now
            or buy_time > decision_at or buy_time.date() >= now.date()):
        return reason + "；退出时钟无效或持仓尚未满足T+1"
    remaining = max(0, int(order.quantity or 0) - int(order.filled_quantity or 0))
    available = await paper._available_sell_amount(db, position, now.date())
    if remaining < 100 or remaining > available:
        return reason + "；退出余量超过原持仓当前可卖数量"
    metadata["exit_version_validation"] = {
        "schema": "position_bound_exit_v1", "position_id": position.id,
        "entry_strategy_version": entry_version,
        "exit_decision_strategy_version": current_version,
        "exit_policy_basis": str(policy.get("basis") or ""),
        "validated_at": now.isoformat(),
    }
    return ""


async def _pending_clock_outcome(db, order, risk_payload, metadata, proof, *, queued=False):
    """Persist a failed preflight only; it must not invent an executed/refused fill."""
    key = "paper_limit_up_queue" if queued else "paper_deferred_order"
    metadata["last_evaluated_round_id"] = proof.get("quote_round_id") or ""
    metadata["last_evaluation_reason"] = proof["reason"]
    risk_payload[key] = metadata
    risk_payload["paper_pending_execution_timing"] = proof
    order.risk_json = _json_dumps(risk_payload)
    order.error_message = proof["reason"]
    await db.commit()
    await db.refresh(order)
    return {"event": "waiting", "reason": proof["reason"], "order": _order_payload(order),
            "risk": risk_payload, "queue" if queued else "deferred": metadata, "fills": []}


async def reconcile_paper_deferred_orders(
    db: AsyncSession,
    *,
    account_id: str,
    round_id: str = "",
    now: datetime | None = None,
    expire_only: bool = False,
) -> list[dict]:
    """在决策后的下一健康报价轮次按五档深度撮合，可部分成交且每轮幂等。"""
    from app.api.v1 import paper

    context = paper._quote_round_context()
    context_round_id = str(context.get("round_id") or "")
    if (not expire_only and round_id and context_round_id
            and str(round_id) != context_round_id):
        # The explicit ID must never relabel the owned quote used for matching.
        # Expiration-only maintenance does not consume a fill frame.
        raise HTTPException(409, "撮合轮次参数与实际行情上下文不一致，禁止重贴成交依据")
    current_round_id = str(round_id or context_round_id)
    observed_at = now
    if observed_at is None and isinstance(context.get("committed_at"), datetime):
        observed_at = context["committed_at"]
    observed_at = observed_at or datetime.now()
    # Expiration must still release pending capacity when quotes are unhealthy.
    orders = list(
        (
            await db.scalars(
                select(TradeOrder)
                .where(
                    TradeOrder.broker == "paper",
                    TradeOrder.account_id == str(account_id or "default"),
                    TradeOrder.status.in_(("submitted", "partial")),
                    TradeOrder.order_type != "after_hours_fixed",
                )
                .order_by(TradeOrder.created_at, TradeOrder.id)
            )
        ).all()
    )
    outcomes: list[dict] = []
    for order in orders:
        # A prior failed fill rolls back and expires every identity-map object.
        await db.refresh(order)
        if order.status not in {"submitted", "partial"}:
            continue
        risk_payload = _json_loads_dict(order.risk_json)
        deferred = risk_payload.get("paper_deferred_order")
        if not isinstance(deferred, dict):
            continue
        if expire_only and not _requires_pending_buy_validity(order):
            continue
        integrity_block = await _block_unreconciled_paper_order(db, order)
        if integrity_block is not None:
            outcomes.append(integrity_block)
            continue
        decision_round_id = str(
            order.decision_round_id
            or deferred.get("decision_round_id")
            or ""
        )
        if order.trade_date != observed_at.date():
            order.status = "canceled"
            order.error_message = "延迟模拟委托跨交易日未成交，自动撤单"
            deferred["last_evaluated_round_id"] = current_round_id
            risk_payload["paper_deferred_order"] = deferred
            order.risk_json = _json_dumps(risk_payload)
            await db.commit()
            await db.refresh(order)
            outcomes.append({
                "event": "canceled",
                "reason": order.error_message,
                "order": _order_payload(order),
                "risk": risk_payload,
                "fills": [],
                "deferred": deferred,
            })
            continue
        time_reason = _pending_buy_time_reason(order, deferred, now=observed_at)
        if time_reason:
            outcomes.append(await _pending_buy_outcome(
                db, order, risk_payload, deferred, status="canceled", reason=time_reason,
                now=observed_at, round_id=current_round_id))
            continue
        if (expire_only or not current_round_id or context.get("quality_status") != "ok"
                or current_round_id == decision_round_id
                or str(deferred.get("last_evaluated_round_id") or "") == current_round_id):
            continue

        version_reason = await _pending_order_version_reason(
            db, order, deferred, now=observed_at,
        )
        if version_reason:
            order.status = "risk_blocked"
            order.risk_level = "block"
            order.error_message = version_reason
            deferred["last_evaluated_round_id"] = current_round_id
            risk_payload["paper_deferred_order"] = deferred
            order.risk_json = _json_dumps(risk_payload)
            await db.commit()
            await db.refresh(order)
            outcomes.append({
                "event": "risk_blocked",
                "reason": order.error_message,
                "order": _order_payload(order),
                "risk": risk_payload,
                "fills": [],
                "deferred": deferred,
            })
            continue

        spot = await _paper_execution_spot(db, order.code)
        quote_ok, quote_reason = paper._execution_quote_status(
            spot,
            observed_at.date(),
            now=observed_at,
        )
        if not quote_ok:
            if _requires_pending_buy_validity(order):
                outcomes.append(await _pending_buy_outcome(
                    db, order, risk_payload, deferred, status="waiting",
                    reason=f"下一轮行情不可撮合：{quote_reason}",
                    now=observed_at, round_id=current_round_id))
                continue
            deferred["last_evaluated_round_id"] = current_round_id
            deferred["last_evaluation_reason"] = quote_reason
            risk_payload["paper_deferred_order"] = deferred
            order.risk_json = _json_dumps(risk_payload)
            order.error_message = f"下一轮行情不可撮合：{quote_reason}"
            await db.commit()
            outcomes.append({
                "event": "waiting",
                "reason": order.error_message,
                "order": _order_payload(order),
                "risk": risk_payload,
                "fills": [],
                "deferred": deferred,
            })
            continue

        validity, validity_reason = await _pending_buy_current_status(
            db, order, deferred, spot, now=observed_at)
        if validity != "valid":
            outcomes.append(await _pending_buy_outcome(
                db, order, risk_payload, deferred, status=validity, reason=validity_reason,
                now=observed_at, round_id=current_round_id))
            continue
        if _requires_pending_buy_validity(order):
            deferred["buy_validity_evaluation"] = {
                "schema": "pending_buy_evaluation_v1", "status": "valid",
                "evaluated_at": observed_at.isoformat(), "quote_round_id": current_round_id,
                "reason": "成交前原路线实时条件仍成立；成交仍需原限价深度及通用风控",
            }

        remaining = max(0, int(order.quantity or 0) - int(order.filled_quantity or 0))
        fill_quantity, fill_price, levels = _depth_fill_plan(
            spot,
            side=str(order.side),
            limit_price=float(order.price),
            remaining_quantity=remaining,
        )
        deferred["last_evaluated_round_id"] = current_round_id
        deferred["depth_levels"] = levels
        if fill_quantity < 100 or fill_price is None:
            deferred["last_evaluation_reason"] = "五档内无满足原限价的一手可见深度"
            risk_payload["paper_deferred_order"] = deferred
            order.risk_json = _json_dumps(risk_payload)
            order.error_message = deferred["last_evaluation_reason"]
            await db.commit()
            outcomes.append({
                "event": "waiting",
                "reason": order.error_message,
                "order": _order_payload(order),
                "risk": risk_payload,
                "fills": [],
                "deferred": deferred,
            })
            continue

        fill_cmd = SubmitOrderCommand(
            code=order.code,
            side=str(order.side),
            price=fill_price,
            quantity=fill_quantity,
            broker="paper",
            account_id=str(order.account_id or "default"),
            order_type=str(order.order_type or "limit"),
            strategy_id=str(order.strategy_id or ""),
            strategy_version=str(order.strategy_version or ""),
            signal_id=str(order.signal_id or order.order_id),
            source=str(order.source or ""),
            reason=str(order.reason or ""),
            execute=True,
            decision_round_id=decision_round_id,
            decision_at=observed_at,
            as_of_at=context.get("as_of_at")
            if isinstance(context.get("as_of_at"), datetime)
            else order.as_of_at,
            config_version=str(order.config_version or ""),
            code_version=str(order.code_version or ""),
            entry_sector_code=deferred.get("entry_sector_code"),
            entry_sector_name=deferred.get("entry_sector_name"),
            deferred_metadata=deferred,
            stop_loss_price=deferred.get("stop_loss_price") if order.account_id == "shared_50k" else None,
            idempotency_key=str(order.idempotency_key or ""),
        )
        fill_risk = await _pre_trade_risk_check(db, fill_cmd)
        block_warn = bool(
            deferred.get("block_warn", str(order.side) == "buy")
        )
        fill_risk_level = _effective_risk_level(fill_risk)
        if fill_risk_level == "block" or (block_warn and fill_risk_level == "warn"):
            messages = fill_risk.get("block_reasons") or fill_risk.get("warnings") or []
            order.status = "risk_blocked"
            order.risk_level = fill_risk_level
            order.error_message = "下一轮撮合前风控复核未通过：" + (
                "；".join(
                    str(item.get("message") or "")
                    for item in messages
                    if item.get("message")
                )
                or "风控状态发生变化"
            )
            risk_payload["deferred_fill_risk"] = fill_risk
            risk_payload["paper_deferred_order"] = deferred
            order.risk_json = _json_dumps(risk_payload)
            await db.commit()
            await db.refresh(order)
            outcomes.append({
                "event": "risk_blocked",
                "reason": order.error_message,
                "order": _order_payload(order),
                "risk": fill_risk,
                "fills": [],
                "deferred": deferred,
            })
            continue

        broker = get_broker_adapter("paper")
        request_id = _paper_fill_request_id(order, round_id=current_round_id)
        # 请求键按“委托+轮次+累计成交量”保持原规则；跨轮撮合前另查
        # 已落账但未写 TradeFill 的旧请求，禁止重复成交或重贴成交时钟。
        if str(order.account_id or "") in paper.PAPER_CHALLENGER_ACCOUNTS:
            if order.side == "sell" and order.strategy_id == "paper-auto-short" and order.source == "position":
                fill_signal_id = f"auto-sell-{request_id}"
            elif (order.account_id == paper.PAPER_ACCOUNT_CHALLENGER_E
                  and order.strategy_id == "paper-auto-short" and order.source == "tenbagger_midline"):
                fill_signal_id = f"auto-tenbagger_midline-{request_id}"
            else:
                fill_signal_id = f"chlg-{request_id}"
        else:
            fill_signal_id = request_id
        from app.trading.paper_public_execution import pending_fill_timing_evidence
        timing_proof = await pending_fill_timing_evidence(
            db, order, deferred, spot, now=observed_at, fill_round_id=current_round_id,
            fill_price=fill_price, fill_quantity=fill_quantity, request_id=request_id)
        if timing_proof["status"] != "validated":
            outcomes.append(await _pending_clock_outcome(db, order, risk_payload, deferred, timing_proof))
            continue
        timing_proof["depth_levels"] = levels
        timing_proof["visible_depth_capacity"] = visible_depth_capacity(spot, side=order.side)
        timing_json = _json_dumps(timing_proof)
        risk_payload["paper_pending_execution_timing"] = timing_proof
        risk_payload["paper_deferred_order"] = deferred
        order.risk_json = _json_dumps(risk_payload)
        try:
            async with _paper_order_transaction(db, order=order) as order_checkpoint:
                if not await _record_paper_account_integrity(db, order, timing_proof, risk_payload):
                    outcomes.append({"event": "risk_blocked", "reason": order.error_message,
                        "order": _order_payload(order), "risk": risk_payload, "fills": [], "deferred": deferred})
                    continue
                capacity = await account_round_depth_evidence(db, order, timing_proof)
                timing_proof["account_round_depth"] = capacity
                fill_risk["paper_account_round_depth"] = capacity
                deferred["capacity_evaluation"] = capacity
                order.risk_json = _json_dumps(risk_payload)
                if capacity["status"] != "validated":
                    order.error_message = capacity["reason"]
                    outcomes.append({"event": "waiting", "reason": order.error_message,
                        "order": _order_payload(order), "risk": fill_risk, "fills": [],
                        "deferred": deferred})
                    continue  # Commit diagnosis under the same lock, not a fabricated fill.
                locked_risk = await _locked_paper_risk_evidence(
                    db, order, fill_cmd, timing_proof, block_warn=block_warn, initial_risk=risk_payload)
                timing_proof["locked_risk"] = locked_risk
                risk_payload["paper_locked_risk"] = locked_risk
                order.risk_json = _json_dumps(risk_payload)
                if locked_risk["status"] != "validated":
                    outcomes.append({"event": "risk_blocked", "reason": order.error_message,
                        "order": _order_payload(order), "risk": locked_risk["result"], "fills": [],
                        "deferred": deferred})
                    continue
                fill_risk = locked_risk["result"]
                timing_json = _json_dumps(timing_proof)
                result = await _dispatch_for_service(
                    broker, db,
                    BrokerOrderRequest(
                        order_id=request_id,
                        code=order.code,
                        side=str(order.side),
                        price=fill_price,
                        quantity=fill_quantity,
                        order_type=str(order.order_type or "limit"),
                        reason=str(order.reason or ""),
                        signal_id=fill_signal_id,
                        strategy_id=str(order.strategy_id or ""),
                        strategy_version=str(order.strategy_version or ""),
                        source=str(order.source or ""),
                        account_name=str(order.account_id or "default"),
                        entry_sector_code=deferred.get("entry_sector_code"),
                        entry_sector_name=deferred.get("entry_sector_name"),
                        stop_loss_price=_to_float(deferred.get("stop_loss_price")),
                        decision_round_id=decision_round_id,
                        fill_round_id=current_round_id,
                        filled_at=datetime.fromisoformat(timing_proof["dispatch_validated_at"]),
                    ),
                    immediate_evidence_json=timing_json,
                )
                if not result.accepted or not result.fills:
                    raise _BrokerDispatchFailure(ValueError(
                        result.error_message or "模拟撮合未返回已接受的真实成交回报"))
                previous_quantity = int(order.filled_quantity or 0)
                previous_value = float(order.avg_fill_price or 0) * previous_quantity
                fill_rows: list[TradeFill] = []
                for fill in result.fills:
                    row = TradeFill(
                        fill_id=fill.fill_id,
                        order_id=order.order_id,
                        broker="paper",
                        external_order_id=result.external_order_id,
                        code=order.code,
                        side=str(order.side),
                        price=fill.price,
                        quantity=fill.quantity,
                        commission=fill.commission,
                        tax=fill.tax,
                        realized_pnl=fill.realized_pnl,
                        broker_trade_id=fill.broker_trade_id,
                        raw_json=_json_dumps({
                            **(fill.raw or {}),
                            "depth_levels": levels,
                            "pending_execution_timing": json.loads(timing_json),
                            "decision_round_id": decision_round_id,
                            "fill_round_id": current_round_id,
                        }),
                        decision_round_id=decision_round_id or None,
                        fill_round_id=current_round_id,
                        trade_date=observed_at.date(),
                        filled_at=fill.filled_at,
                    )
                    db.add(row)
                    fill_rows.append(row)

                added_quantity = sum(int(fill.quantity) for fill in result.fills)
                added_value = sum(float(fill.price) * int(fill.quantity) for fill in result.fills)
                total_quantity = previous_quantity + added_quantity
                order.filled_quantity = total_quantity
                order.avg_fill_price = (
                    round((previous_value + added_value) / total_quantity, 4)
                    if total_quantity
                    else None
                )
                order.status = (
                    "filled"
                    if total_quantity >= int(order.quantity or 0)
                    else "partial"
                )
                order.external_order_id = result.external_order_id or order.external_order_id
                order.last_fill_round_id = current_round_id
                order.risk_level = fill_risk.get("final_level", order.risk_level)
                order.error_message = (
                    None
                    if order.status == "filled"
                    else f"本轮按五档成交{added_quantity}股，剩余{int(order.quantity) - total_quantity}股"
                )
                deferred.update({
                    "last_evaluated_round_id": current_round_id,
                    "last_fill_round_id": current_round_id,
                    "last_fill_quantity": added_quantity,
                    "last_fill_price": fill_price,
                })
                risk_payload["paper_deferred_order"] = deferred
                risk_payload["deferred_fill_risk"] = fill_risk
                order.risk_json = _json_dumps(risk_payload)
        except _BrokerDispatchFailure as failure:
            risk_payload["paper_deferred_order"] = deferred
            await _reject_unchanged_paper_order(db, order, order_checkpoint,
                reason=f"下一轮模拟撮合失败：{failure.error}", risk_json=_json_dumps(risk_payload))
            outcomes.append({
                "event": "rejected",
                "reason": order.error_message,
                "order": _order_payload(order),
                "risk": fill_risk,
                "fills": [],
                "deferred": deferred,
            })
            continue

        await db.refresh(order)
        for row in fill_rows:
            await db.refresh(row)
        outcomes.append({
            "event": order.status,
            "reason": (
                "下一轮按五档深度全部成交"
                if order.status == "filled"
                else order.error_message
            ),
            "order": _order_payload(order),
            "risk": fill_risk,
            "fills": [_fill_payload(row) for row in fill_rows],
            "depth_levels": levels,
            "deferred": deferred,
        })

    return outcomes


async def reconcile_paper_limit_up_orders(
    db: AsyncSession,
    *,
    account_id: str,
    now: datetime | None = None,
    expire_only: bool = False,
) -> list[dict]:
    """保守撮合 paper 涨停排队单。

    成交证据仅接受两类：
    1. 行情已开板且原限价内合法五档可见量覆盖整笔（不做无量补单）；
    2. 仍封板，但排队后的新增成交手数覆盖“下单时买一队列 + 本单”。

    第二类是基于腾讯成交量(手)与买一量(手)的 FIFO 近似。撤单造成的排位改善
    不提前计入，宁可少成交也不制造虚假成交。
    """
    from app.api.v1 import paper

    round_context = paper._quote_round_context()
    now = (
        now
        or (
            round_context.get("committed_at")
            if isinstance(round_context.get("committed_at"), datetime)
            else None
        )
        or datetime.now()
    )
    current_round_id = str(round_context.get("round_id") or "")
    orders = (
        await db.execute(
            select(TradeOrder)
            .where(
                TradeOrder.broker == "paper",
                TradeOrder.account_id == str(account_id or "default"),
                TradeOrder.side == "buy",
                TradeOrder.status == "submitted",
                TradeOrder.order_type != "after_hours_fixed",
            )
            .order_by(TradeOrder.created_at, TradeOrder.id)
        )
    ).scalars().all()
    outcomes: list[dict] = []

    for order in orders:
        await db.refresh(order)
        if order.status != "submitted":
            continue
        risk_payload = _json_loads_dict(order.risk_json)
        queue = risk_payload.get("paper_limit_up_queue")
        if not isinstance(queue, dict):
            continue
        if expire_only and not _requires_pending_buy_validity(order):
            continue
        integrity_block = await _block_unreconciled_paper_order(db, order, queued=True)
        if integrity_block is not None:
            outcomes.append(integrity_block)
            continue

        from app.api.v1 import paper

        queued_strategy_version = str(queue.get("strategy_version") or "")
        current_strategy_version = paper._strategy_version(
            str(order.account_id or "default")
        )
        from app.paper.portfolio_contract import PORTFOLIO_ACCOUNT
        portfolio_version_reason = ""
        if order.account_id == PORTFOLIO_ACCOUNT:
            portfolio_version_reason = await _pending_order_version_reason(db, order, queue, now=now)
            if not portfolio_version_reason:
                current_strategy_version = str(order.strategy_version or "")
        if (
            not queued_strategy_version
            or queued_strategy_version != current_strategy_version
            or portfolio_version_reason
        ):
            order.status = "risk_blocked"
            order.error_message = (
                "排队委托策略版本已变化或缺失："
                f"queued={queued_strategy_version or 'legacy_unversioned'}，"
                f"current={current_strategy_version}；禁止跨版本成交"
                + (f"；{portfolio_version_reason}" if portfolio_version_reason else "")
            )
            queue["current_strategy_version"] = current_strategy_version
            risk_payload["paper_limit_up_queue"] = queue
            order.risk_level = "block"
            order.risk_json = _json_dumps(risk_payload)
            await db.commit()
            await db.refresh(order)
            outcomes.append({
                "event": "risk_blocked",
                "reason": order.error_message,
                "order": _order_payload(order),
                "risk": risk_payload,
                "queue": queue,
                "fills": [],
            })
            continue

        cutoff = _parse_queue_cutoff(
            queue.get("cancel_time")
            or getattr(settings, "PAPER_HIGHBOARD_QUEUE_CANCEL_TIME", "14:50")
        )
        if order.trade_date != now.date() or now.time() > cutoff:
            order.status = "canceled"
            order.error_message = (
                "涨停排队委托跨日自动撤单"
                if order.trade_date != now.date()
                else f"涨停排队至{cutoff.strftime('%H:%M')}仍未成交，自动撤单"
            )
            await db.commit()
            await db.refresh(order)
            outcomes.append({
                "event": "canceled",
                "reason": order.error_message,
                "order": _order_payload(order),
                "risk": risk_payload,
                "queue": queue,
                "fills": [],
            })
            continue

        time_reason = _pending_buy_time_reason(order, queue, now=now)
        if time_reason:
            outcomes.append(await _pending_buy_outcome(
                db, order, risk_payload, queue, queued=True, status="canceled",
                reason=time_reason, now=now, round_id=current_round_id))
            continue
        # Validate frozen evidence before any new quote can conceal or repair it.
        original_hands = [
            _to_float(queue.get(key)) for key in (
                "baseline_volume_hands", "queue_ahead_hands", "order_hands", "last_volume_hands")
        ]
        if (queue.get("queue_evidence_contract") != LIMIT_QUEUE_EVIDENCE_CONTRACT_VERSION
                or any(value is None or value < 0 or not value.is_integer()
                       for value in original_hands)
                or original_hands[2] != max(1, (int(order.quantity) + 99) // 100)
                or original_hands[3] < original_hands[0]):
            outcomes.append(await _pending_buy_outcome(
                db, order, risk_payload, queue, queued=True, status="canceled",
                reason="queue_evidence_invalid：原队列证据合同缺失或非法，撤销余量，不回填基线",
                now=now, round_id=current_round_id))
            continue
        baseline_volume, queue_ahead, order_hands, last_volume = (
            int(value) for value in original_hands)
        if (expire_only
                or (_requires_pending_buy_validity(order) and not current_round_id)
                or (current_round_id and round_context.get("quality_status") != "ok")):
            continue
        if (current_round_id and (current_round_id == order.decision_round_id
                or queue.get("last_evaluated_round_id") == current_round_id)):
            continue

        spot = await _paper_execution_spot(db, order.code)
        quote_ok, quote_reason = paper._execution_quote_status(
            spot,
            now.date(),
            now=now,
        )
        if not quote_ok:
            if _requires_pending_buy_validity(order):
                outcomes.append(await _pending_buy_outcome(
                    db, order, risk_payload, queue, queued=True, status="waiting",
                    reason=f"排队委托等待有效实时行情：{quote_reason}",
                    now=now, round_id=current_round_id))
                continue
            outcomes.append({
                "event": "waiting",
                "reason": f"排队委托等待有效实时行情：{quote_reason}",
                "order": _order_payload(order),
                "risk": risk_payload,
                "queue": queue,
                "fills": [],
            })
            continue

        # A known volume regression is terminal even if another route leaf is
        # unknown. Record healthy cumulative observations without refreshing TTL.
        current_volume = _to_float(getattr(spot, "volume", None))
        current_volume_valid = (
            current_volume is not None and current_volume >= 0 and current_volume.is_integer())
        if current_volume_valid:
            current_volume = int(current_volume)
            if current_volume < last_volume:
                outcomes.append(await _pending_buy_outcome(
                    db, order, risk_payload, queue, queued=True, status="canceled",
                    reason="queue_volume_regressed：累计量早于原基线或已观测量，禁止重置后补成交",
                    now=now, round_id=current_round_id))
                continue
            queue["last_volume_hands"] = current_volume
        validity, validity_reason = await _pending_buy_current_status(db, order, queue, spot, now=now)
        if validity != "valid":
            outcomes.append(await _pending_buy_outcome(
                db, order, risk_payload, queue, queued=True, status=validity,
                reason=validity_reason, now=now, round_id=current_round_id))
            continue
        if _requires_pending_buy_validity(order):
            queue["buy_validity_evaluation"] = {
                "schema": "pending_buy_evaluation_v1", "status": "valid",
                "evaluated_at": now.isoformat(), "quote_round_id": current_round_id,
                "reason": "原路线有效；封板排队仍须成交量覆盖而非假定卖一成交",
            }
        limit_up = _to_float(queue.get("limit_up_price")) or _to_float(getattr(spot, "limit_up", None))
        last = _to_float(getattr(spot, "price", None))
        if not limit_up or not last or limit_up <= 0 or last <= 0:
            if _requires_pending_buy_validity(order):
                outcomes.append(await _pending_buy_outcome(
                    db, order, risk_payload, queue, queued=True, status="waiting",
                    reason="排队委托缺少有效涨停价或最新价",
                    now=now, round_id=current_round_id))
                continue
            outcomes.append({
                "event": "waiting",
                "reason": "排队委托缺少有效涨停价或最新价",
                "order": _order_payload(order),
                "risk": risk_payload,
                "queue": queue,
                "fills": [],
            })
            continue

        if not current_volume_valid:
            outcomes.append(await _pending_buy_outcome(
                db, order, risk_payload, queue, queued=True, status="waiting",
                reason="queue_volume_unknown：本帧累计量缺失或非法，等待有效证据，原确认不续期",
                now=now, round_id=current_round_id))
            continue
        traded_after_queue = current_volume - baseline_volume
        cover_ratio = max(
            1.0,
            float(getattr(settings, "PAPER_HIGHBOARD_QUEUE_VOLUME_COVER_RATIO", 1.0) or 1.0),
        )
        required_hands = int(math.ceil(queue_ahead * cover_ratio)) + order_hands
        sealed = _is_sealed_limit_up_quote(spot)
        fill_trigger = ""
        fill_price: float | None = None

        open_depth = None
        if not sealed:
            from app.trading.paper_public_execution import queue_open_fill_evidence

            open_depth = queue_open_fill_evidence(
                spot, limit_price=float(order.price), quantity=order.quantity)
            open_depth["quote_round_id"] = current_round_id
            open_depth["evaluated_at"] = now.isoformat()
            queue["open_depth_evaluation"] = open_depth
            if open_depth["status"] == "fillable":
                # 可见数量先验收，仍按原限价记账，不给更低现价的虚假优势。
                fill_price = float(order.price)
                fill_trigger = "board_opened"
        elif traded_after_queue >= required_hands:
            fill_price = float(order.price)
            fill_trigger = "queue_turnover_covered"

        if fill_price is None:
            if open_depth is not None and not _requires_pending_buy_validity(order):
                # Preserve the failed quantity diagnosis without touching any book.
                risk_payload["paper_limit_up_queue"] = queue
                order.risk_json = _json_dumps(risk_payload)
                await db.commit()
            if _requires_pending_buy_validity(order):
                queue.update(traded_after_queue_hands=traded_after_queue,
                             required_hands=required_hands)
                outcomes.append(await _pending_buy_outcome(
                    db, order, risk_payload, queue, queued=True, status="waiting",
                    reason=(f"涨停排队中：排队后成交{traded_after_queue}手/需覆盖{required_hands}手"
                            if sealed else open_depth["reason"]),
                    now=now, round_id=current_round_id))
                continue
            outcomes.append({
                "event": "waiting",
                "reason": (
                    f"涨停排队中：排队后成交{traded_after_queue}手/需覆盖{required_hands}手"
                    if sealed
                    else open_depth["reason"]
                ),
                "order": _order_payload(order),
                "risk": risk_payload,
                "queue": {
                    **queue,
                    "traded_after_queue_hands": traded_after_queue,
                    "required_hands": required_hands,
                },
                "fills": [],
            })
            continue

        fill_cmd = SubmitOrderCommand(
            code=order.code,
            side="buy",
            price=fill_price,
            quantity=int(order.quantity),
            broker="paper",
            account_id=str(order.account_id or "default"),
            order_type=order.order_type,
            strategy_id=str(order.strategy_id or ""),
            strategy_version=queued_strategy_version,
            signal_id=str(order.signal_id or order.order_id),
            source=str(order.source or ""),
            reason=str(order.reason or ""),
            execute=True,
            entry_sector_code=queue.get("entry_sector_code"),
            entry_sector_name=queue.get("entry_sector_name"),
            queue_metadata=queue,
            idempotency_key=str(order.idempotency_key or ""),
            decision_round_id=str(
                order.decision_round_id or queue.get("decision_round_id") or ""
            ),
            decision_at=now,
            as_of_at=(
                round_context.get("as_of_at")
                if isinstance(round_context.get("as_of_at"), datetime)
                else order.as_of_at
            ),
        )
        queue_stop_loss = _to_float(queue.get("stop_loss_price"))
        if queue_stop_loss and queue_stop_loss > 0:
            # 路线级止损价已写入 queue metadata；延迟撮合后必须把它落到中台委托，
            # 否则 _apply_auto_position_risk 会改写为通用默认值，破坏路线专属退出。
            fill_cmd.stop_loss_price = queue_stop_loss
        fill_risk = await _pre_trade_risk_check(db, fill_cmd)
        block_warn = bool(queue.get("block_warn", True))
        fill_risk_level = _effective_risk_level(fill_risk)
        if fill_risk_level == "block" or (
            block_warn
            and getattr(settings, "PAPER_AUTO_WARN_RISK_BLOCK_BUY", True)
            and fill_risk_level == "warn"
        ):
            order.status = "risk_blocked"
            messages = fill_risk.get("block_reasons") or fill_risk.get("warnings") or []
            order.error_message = "排队成交前复核未通过：" + (
                "；".join(str(item.get("message") or "") for item in messages if item.get("message"))
                or "风控状态发生变化"
            )
            queue["fill_trigger"] = fill_trigger
            risk_payload["paper_limit_up_queue"] = queue
            risk_payload["queue_fill_risk"] = fill_risk
            order.risk_level = fill_risk_level
            order.risk_json = _json_dumps(risk_payload)
            await db.commit()
            await db.refresh(order)
            outcomes.append({
                "event": "risk_blocked",
                "reason": order.error_message,
                "order": _order_payload(order),
                "risk": fill_risk,
                "queue": queue,
                "fills": [],
            })
            continue

        broker = get_broker_adapter("paper")
        queue_fill_request_id = _paper_fill_request_id(order, queued=True)
        from app.trading.paper_public_execution import pending_fill_timing_evidence
        timing_proof = await pending_fill_timing_evidence(
            db, order, queue, spot, now=now, fill_round_id=current_round_id,
            fill_price=fill_price, fill_quantity=int(order.quantity), request_id=queue_fill_request_id,
            queue_cancel_at=datetime.combine(order.trade_date, cutoff))
        if timing_proof["status"] != "validated":
            outcomes.append(await _pending_clock_outcome(
                db, order, risk_payload, queue, timing_proof, queued=True))
            continue
        if open_depth is not None:
            timing_proof["queue_open_depth"] = open_depth
            timing_proof["visible_depth_capacity"] = visible_depth_capacity(spot, side="buy")
        timing_json = _json_dumps(timing_proof)
        risk_payload["paper_pending_execution_timing"] = timing_proof
        risk_payload["paper_limit_up_queue"] = queue
        order.risk_json = _json_dumps(risk_payload)
        async with _paper_order_transaction(db, order=order):
            if not await _record_paper_account_integrity(db, order, timing_proof, risk_payload):
                outcomes.append({"event": "risk_blocked", "reason": order.error_message,
                    "order": _order_payload(order), "risk": risk_payload, "fills": [], "queue": queue})
                continue
            if open_depth is not None:
                capacity = await account_round_depth_evidence(db, order, timing_proof)
                timing_proof["account_round_depth"] = capacity
                fill_risk["paper_account_round_depth"] = capacity
                queue["capacity_evaluation"] = capacity
                order.risk_json = _json_dumps(risk_payload)
                if capacity["status"] != "validated":
                    order.error_message = capacity["reason"]
                    outcomes.append({"event": "waiting", "reason": order.error_message,
                        "order": _order_payload(order), "risk": fill_risk, "fills": [], "queue": queue})
                    continue
            locked_risk = await _locked_paper_risk_evidence(
                db, order, fill_cmd, timing_proof,
                block_warn=block_warn and getattr(settings, "PAPER_AUTO_WARN_RISK_BLOCK_BUY", True),
                initial_risk=risk_payload)
            timing_proof["locked_risk"] = locked_risk
            risk_payload["paper_locked_risk"] = locked_risk
            order.risk_json = _json_dumps(risk_payload)
            if locked_risk["status"] != "validated":
                outcomes.append({"event": "risk_blocked", "reason": order.error_message,
                    "order": _order_payload(order), "risk": locked_risk["result"], "fills": [], "queue": queue})
                continue
            fill_risk = locked_risk["result"]
            timing_json = _json_dumps(timing_proof)
            result = await _dispatch_broker_order(
                broker, db,
                BrokerOrderRequest(
                    order_id=queue_fill_request_id,
                    code=order.code,
                    side="buy",
                    price=fill_price,
                    quantity=int(order.quantity),
                    order_type=order.order_type,
                    reason=str(order.reason or ""),
                    signal_id=(f"auto-tenbagger_midline-{queue_fill_request_id}"
                               if order.account_id == paper.PAPER_ACCOUNT_CHALLENGER_E else queue_fill_request_id),
                    strategy_id=str(order.strategy_id or ""),
                    strategy_version=queued_strategy_version,
                    source=str(order.source or ""),
                    account_name=str(order.account_id or "default"),
                    entry_sector_code=queue.get("entry_sector_code"),
                    entry_sector_name=queue.get("entry_sector_name"),
                    stop_loss_price=queue_stop_loss,
                    decision_round_id=str(
                        order.decision_round_id or queue.get("decision_round_id") or ""
                    ),
                    fill_round_id=current_round_id,
                    filled_at=datetime.fromisoformat(timing_proof["dispatch_validated_at"]),
                ),
                immediate_evidence_json=timing_json,
            )
            if not result.accepted or not result.fills:
                raise ValueError(result.error_message or "排队撮合缺少真实成交回报，不得确认成交")
            order.external_order_id = result.external_order_id or order.external_order_id
            order.status = result.status
            order.error_message = result.error_message or None
            order.risk_level = fill_risk.get("final_level", order.risk_level)
            queue.update({
                "fill_trigger": fill_trigger,
                "filled_at": max(fill.filled_at for fill in result.fills).isoformat(sep=" "),
                "traded_after_queue_hands": traded_after_queue,
                "required_hands": required_hands,
            })
            risk_payload["paper_limit_up_queue"] = queue
            risk_payload["queue_fill_risk"] = fill_risk
            order.risk_json = _json_dumps(risk_payload)

            fill_rows: list[TradeFill] = []
            if result.fills:
                total_qty = sum(fill.quantity for fill in result.fills)
                total_value = sum(fill.price * fill.quantity for fill in result.fills)
                order.filled_quantity = total_qty
                order.avg_fill_price = round(total_value / total_qty, 4) if total_qty else None
                order.last_fill_round_id = current_round_id or None
                for fill in result.fills:
                    row = TradeFill(
                        fill_id=fill.fill_id,
                        order_id=order.order_id,
                        broker="paper",
                        external_order_id=order.external_order_id,
                        code=order.code,
                        side="buy",
                        price=fill.price,
                        quantity=fill.quantity,
                        commission=fill.commission,
                        tax=fill.tax,
                        realized_pnl=fill.realized_pnl,
                        broker_trade_id=fill.broker_trade_id,
                        raw_json=_json_dumps({**(fill.raw or {}), "queue_fill_trigger": fill_trigger,
                                             "pending_execution_timing": json.loads(timing_json)}),
                        decision_round_id=str(
                            order.decision_round_id or queue.get("decision_round_id") or ""
                        ) or None,
                        fill_round_id=current_round_id or None,
                        trade_date=now.date(),
                        filled_at=fill.filled_at,
                    )
                    db.add(row)
                    fill_rows.append(row)

        await db.refresh(order)
        for row in fill_rows:
            await db.refresh(row)
        event = "filled" if order.status == "filled" else "rejected"
        reason = (
            "涨停排队成交：盘中开板后按原涨停限价保守撮合"
            if fill_trigger == "board_opened"
            else f"涨停排队成交：新增成交{traded_after_queue}手已覆盖前方队列{required_hands}手"
        )
        outcomes.append({
            "event": event,
            "reason": order.error_message or reason,
            "order": _order_payload(order),
            "risk": fill_risk,
            "queue": queue,
            "fills": [_fill_payload(row) for row in fill_rows],
        })

    return outcomes


async def cancel_reduction_for_protective_exit(
    db: AsyncSession, *, account_name: str, position, quantity: int,
    candidate: dict, decision_at: datetime, decision_round_id: str,
    strategy_id: str, source: str,
) -> dict:
    """Cancel only the remainder of one bound T reduction; never reprice/fill it.

    This internal coordination is not an order endpoint or execution permission.
    The caller must submit the replacement through submit_order and its original
    risk/quote/T+1 chain. Cancellation and replacement are deliberately distinct
    facts: a failed new submission does not undo or invent a prior fill.
    """
    from app.api.v1 import paper
    from app.models.paper import PaperAccount, PaperPosition
    from app.paper.experiment import EXPERIMENT_ACCOUNTS, EXIT_UPGRADE_CONTRACT_VERSION
    from app.paper.account_policy import ROUTE_ACCOUNT_NAMES

    # Both existing internal exit producers may protect the same bound holding.
    # Do not recreate the former challenger/watchdog source-label deadlock.
    origins = {("paper-auto-short", "position")}
    origins.update(("paper-challenger-forward", route)
                   for route, name in ROUTE_ACCOUNT_NAMES.items() if name == account_name)

    def blocked(code):
        return {"status": "blocked", "reason_code": code}

    reason = str(candidate.get("exit_trigger_reason") or "")
    if (account_name not in EXPERIMENT_ACCOUNTS
            or not paper._is_full_exit_reason(reason)
            or type(quantity) is not int or quantity < 100 or quantity % 100
            or not decision_round_id):
        return blocked("not_full_protective_exit")
    query = select(TradeOrder).where(
        TradeOrder.broker == "paper", TradeOrder.account_id == account_name,
        TradeOrder.code == position.code, TradeOrder.side == "sell",
        TradeOrder.status.in_(("pending", "submitted", "partial")),
    )
    orders = list((await db.scalars(query)).all())
    if len(orders) != 1:
        return blocked("ambiguous_active_sells")
    order = orders[0]
    # Freeze only decision-relevant position leaves before waiting for the fill lock.
    fields = ("id", "account_id", "code", "buy_amount", "buy_price", "buy_time",
              "strategy_version", "stop_loss_price", "is_closed")
    expected_position = tuple(getattr(position, key) for key in fields)
    async with _paper_order_transaction(db, order=order):
        peers = list((await db.scalars(query.with_only_columns(TradeOrder.id))).all())
        await db.refresh(position)
        if peers != [order.id] or tuple(getattr(position, key) for key in fields) != expected_position:
            return blocked("position_or_orders_changed")
        accounts = list((await db.scalars(select(PaperAccount.id).where(
            PaperAccount.account_name == account_name, PaperAccount.status == "active"))).all())
        positions = list((await db.scalars(select(PaperPosition.id).where(
            PaperPosition.account_id == position.account_id,
            PaperPosition.code == position.code, PaperPosition.is_closed.is_(False)))).all())
        if accounts != [position.account_id] or positions != [position.id]:
            return blocked("position_account_identity")
        now = paper._public_order_clock()
        old_at = _pending_buy_clock(order.decision_at)
        created_at = _pending_buy_clock(order.created_at)
        buy_at = _pending_buy_clock(position.buy_time)
        if (not isinstance(now, datetime) or now.tzinfo is not None
                or not isinstance(decision_at, datetime) or decision_at.tzinfo is not None
                or old_at is None or created_at is None or buy_at is None
                or not old_at <= created_at < decision_at <= now
                or order.trade_date != decision_at.date() or now.date() != order.trade_date
                or buy_at.date() >= now.date()
                or decision_round_id == order.decision_round_id):
            return blocked("exit_upgrade_clock_invalid")
        context = paper._quote_round_context()
        spot = await _paper_execution_spot(db, position.code)
        quote_ok, _ = paper._execution_quote_status(spot, now.date(), now=now)
        if (context.get("round_id") != decision_round_id or context.get("quality_status") != "ok"
                or not quote_ok or paper._conservative_execution_price(spot, "sell") is None):
            return blocked("exit_upgrade_quote_invalid")
        risk = _json_loads_dict(order.risk_json)
        deferred = risk.get("paper_deferred_order")
        old_candidate = deferred.get("candidate") if isinstance(deferred, dict) else None
        policy = candidate.get("exit_policy")
        parameters = candidate.get("exit_parameters")
        if (order.status not in {"submitted", "partial"} or order.order_type != "limit"
                or (order.status == "submitted"
                    and order.external_order_id != f"paper-deferred-{order.order_id}")
                or (order.strategy_id, order.source) not in origins
                or (strategy_id, source) not in origins
                or not position.strategy_version or order.strategy_version != position.strategy_version
                or not isinstance(old_candidate, dict)
                or deferred.get("position_id") != position.id
                or deferred.get("strategy_version") != order.strategy_version
                or deferred.get("decision_round_id") != order.decision_round_id
                or _pending_buy_clock(deferred.get("decision_at")) != old_at
                or not str(deferred.get("exit_decision_strategy_version") or "")
                or not isinstance(policy, dict)
                or policy.get("position_strategy_version") != position.strategy_version
                or old_candidate.get("exit_policy") != policy
                or not isinstance(parameters, dict) or not parameters
                or old_candidate.get("exit_parameters") != parameters
                or not str(old_candidate.get("exit_trigger_reason") or "")
                or paper._is_full_exit_reason(str(old_candidate.get("exit_trigger_reason") or ""))
                or not str(order.reason or "").startswith(f"T减仓{order.quantity}股：")):
            return blocked("not_bound_partial_reduction")
        available = await paper._available_sell_amount(db, position, now.date())
        original_available = old_candidate.get("available_sell_amount")
        filled = order.filled_quantity
        if (type(filled) is not int or filled < 0 or filled % 100
                or type(order.quantity) is not int or order.quantity % 100
                or type(original_available) is not int or order.quantity >= original_available
                or (order.status == "submitted" and filled != 0)
                or (order.status == "partial" and filled == 0)
                or not 100 <= order.quantity - filled < quantity
                or available != quantity or candidate.get("available_sell_amount") != available):
            return blocked("reduction_or_available_quantity_changed")
        # Both directions of ledger/receipt identity must agree before releasing
        # a pending remainder. This original read-only checker runs under the lock.
        integrity = await account_execution_integrity_evidence(
            db, order, {"quote_round_id": decision_round_id})
        receipts = (await db.execute(select(
            TradeFill.quantity, TradeFill.external_order_id, TradeFill.fill_id, TradeFill.fill_round_id,
        ).where(TradeFill.order_id == order.order_id))).all()
        if (integrity.get("status") != "validated"
                or any(type(r.quantity) is not int or r.quantity < 100 or r.quantity % 100 for r in receipts)
                or sum(r.quantity for r in receipts) != filled):
            return blocked("exit_upgrade_receipt_integrity")
        if order.status == "partial":
            # The real producer replaces paper-deferred-* with its last booked
            # paper-pf-* ID. Verify that exact receipt, not an arbitrary prefix.
            latest = [r for r in receipts if r.fill_round_id == order.last_fill_round_id]
            if (len(latest) != 1 or not order.last_fill_round_id
                    or latest[0].external_order_id != order.external_order_id
                    or order.external_order_id != "paper-" + str(latest[0].fill_id).removeprefix("fill-")):
                return blocked("exit_upgrade_last_fill_identity")
        completed_at = paper._public_order_clock()
        if (not isinstance(completed_at, datetime) or completed_at.tzinfo is not None
                or completed_at < now or completed_at.date() != now.date()
                or not paper._execution_quote_status(spot, now.date(), now=completed_at)[0]):
            return blocked("exit_upgrade_quote_expired_under_lock")
        proof = {
            "contract_version": EXIT_UPGRADE_CONTRACT_VERSION, "status": "canceled",
            "old_order_id": order.order_id, "position_id": position.id,
            "original_quantity": order.quantity, "filled_quantity": filled,
            "canceled_remaining_quantity": order.quantity - filled,
            "decision_round_id": decision_round_id, "decision_at": decision_at.isoformat(),
            "canceled_at": completed_at.isoformat(), "trigger_reason": reason,
            "replacement_quantity": quantity, "replacement_status": "requires_new_submission",
            "exit_decision_strategy_version": paper._strategy_version(account_name),
            "account_execution_integrity": integrity,
        }
        risk["paper_exit_upgrade"] = proof
        order.status = "canceled"
        order.error_message = "旧T减仓未成交余量已撤销；新保护性退出仍须独立风控及真实后续报价"
        order.risk_json = _json_dumps(risk)
    return proof


async def _cancel_paper_after_hours_intent(db, order, *, reason_kind):
    """Cancel only verified remainder in the SAME lock/transaction as matching.

    No broker, quote refresh, fee-policy check or resource release. Historical
    fills require the whole actual partial root, never a projection/JSON label.
    """
    from app.api.v1 import paper
    from app.trading import paper_after_hours_resources as resources
    from app.trading import paper_after_hours_allocation as allocator
    from app.trading.paper_after_hours_execution import MODE, fifo_key
    if reason_kind not in {"user_cancel", "session_end"}:
        raise HTTPException(409, "盘后撤单原因不合法")
    if db.new or db.dirty or db.deleted:
        raise HTTPException(409, "盘后撤单要求独立干净事务")
    # No order checkpoint before waiting: cancellation deliberately rereads the
    # latest remaining quantity UNDER the lock (a matching task may finish first).
    async with _paper_order_transaction(db):
        await db.refresh(order)
        if order.order_type != MODE or order.broker != "paper":
            raise HTTPException(409, "盘后撤单模式不匹配")
        if order.status in {"filled", "canceled", "risk_blocked", "rejected", "accepted"}:
            return {"order": _order_payload(order), "status": "unchanged", "reason": "当前状态不可撤单"}
        try:
            fifo_key(order)
            risk = resources._object(order.risk_json, reject_duplicate_keys=True)
            intent = risk["paper_after_hours_intent"]
            now = allocator._clock(paper._public_order_clock())
            end = datetime.combine(order.trade_date, time(15, 30))
            floor = allocator._clock(intent["terminal_validated_at"])
            filled = allocator._integer(order.filled_quantity, zero=True)
            if (allocator._integer(order.quantity) % 100 or filled % 100
                    or not filled < order.quantity
                    or order.status != ("partial" if filled else "submitted")):
                raise ValueError("cancel_original_state_or_quantity_conflict")
            if now < floor:
                raise ValueError("cancel_clock_rollback")
            if reason_kind == "session_end" and now < end:
                return {"order": _order_payload(order), "status": "unchanged", "reason": "会话尚未结束"}
            actual_count = await db.scalar(select(func.count(TradeFill.id)).where(
                TradeFill.order_id == order.order_id))
            durable_count = await db.scalar(select(func.count()).select_from(resources.Receipt).where(
                resources.Receipt.order_id == order.order_id))
            receipt_refs = []
            if filled:
                # Retrospective frozen fees / source clocks, not current policy
                # or NEW-fill TTL; closing a session cannot erase old consumption.
                roots = await resources._verified_partial_receipt_bindings(db,
                    account_numeric_id=intent["numeric_account_id"], code=order.code,
                    trade_date=order.trade_date, cutoff=now)
                bindings = [value for value in roots.values() if value["order_id"] == order.order_id]
                if (not bindings or len(bindings) != actual_count or durable_count != actual_count
                        or sum(value["quantity"] for value in bindings) != filled):
                    raise ValueError("cancel_partial_receipts_or_projection_conflict")
                floor = max(floor, allocator._clock(order.updated_at),
                    allocator._clock(risk["paper_after_hours_consumption"]["terminal_checked_at"]),
                    *(value["filled_at"] for value in bindings))
                if now < floor:
                    raise ValueError("cancel_clock_before_previous_service_terminal")
                receipt_refs = sorted(value["fill_id"] for value in bindings)
            elif actual_count or durable_count:
                raise ValueError("cancel_zero_intent_has_unverified_fills_or_consumption")
            # Recheck after all asynchronous reads; never backdate the cancellation.
            checked = allocator._clock(paper._public_order_clock())
            if checked < max(now, floor):
                raise ValueError("cancel_clock_rollback_after_history")
        except (ValueError, KeyError, TypeError, AttributeError, RecursionError, OverflowError) as exc:
            raise HTTPException(409, "盘后原意图、经济回报或撤单时钟未核验，禁止覆盖") from exc
        frozen = {key: getattr(order, key) for key in PAPER_ORDER_CHECKPOINT_FIELDS}
        external_id, updated_at = order.external_order_id, order.updated_at
        effective_reason = "session_end" if checked >= end else "user_cancel"
        risk["paper_after_hours_cancel"] = {
            "contract_version": allocator.PARTIAL_CANCEL_OBSERVATION_PROTOCOL,
            "order_id": order.order_id, "original_quantity": order.quantity,
            "reason": effective_reason, "canceled_at": checked.isoformat(),
            "original_session_end_at": end.isoformat(),
            "unfilled_quantity": order.quantity - filled, "filled_quantity_preserved": filled,
            "verified_fill_ids": receipt_refs,
            "simulation_only": True, "exchange_cancel_receipt": None,
        }
        message = ("盘后固定价格会话结束，未成交余量失效" if effective_reason == "session_end"
                   else "用户撤销盘后固定价格未成交余量")
        table = TradeOrder.__table__
        result = await db.execute(table.update().where(*(
            table.c[key] == value for key, value in frozen.items()
        ), table.c.external_order_id == external_id, table.c.updated_at == updated_at).values(
            status="canceled", error_message=message, risk_json=_json_dumps(risk), updated_at=checked))
        if result.rowcount != 1:
            raise HTTPException(409, "撤单竞争导致委托已变化，禁止覆盖")
        await db.refresh(order)
        try:
            terminal = allocator._clock(paper._public_order_clock())
        except (ValueError, TypeError) as exc:
            raise HTTPException(409, "盘后撤单CAS后时钟非法，禁止提交") from exc
        if terminal < checked:
            raise HTTPException(409, "盘后撤单CAS后时钟回退，禁止提交")
        answer = {"order": _order_payload(order), "status": "canceled", "reason": message}
    # The original transaction owns COMMIT; a lost ACK remains uncertain, not an
    # invented rejection/undo. physical_commit_at is deliberately not asserted.
    return answer


async def _after_hours_partial_context(db, order, priors, *, now):
    """Whole original-order DB context; missing receipts never become zero history."""
    from sqlalchemy import func
    from app.trading import paper_after_hours_resources as resources
    from app.trading import paper_after_hours_allocation as allocator
    from app.trading.paper_after_hours_execution import MODE
    ids = list((await db.scalars(select(TradeOrder.id).where(
        TradeOrder.account_id == order.account_id, TradeOrder.code == order.code,
        TradeOrder.trade_date == order.trade_date, TradeOrder.order_type == MODE)
        .order_by(TradeOrder.id).limit(allocator.MAX_LOCAL_ORDERS + 1))).all())
    if not 1 <= len(ids) <= allocator.MAX_LOCAL_ORDERS:
        raise HTTPException(409, "分片完整原委托队列超出读取预算")
    quantities, counts, context, read_bytes = {}, {}, [], 0
    for prior in priors:
        name = prior["order_id"]
        quantities[name] = quantities.get(name, 0) + prior["quantity"]
        counts[name] = counts.get(name, 0) + 1
    for identifier in ids:
        peer = await resources._book_row(db, TradeOrder, TradeOrder.id == identifier,
            max_text_bytes=resources.MAX_READ_BYTES - read_bytes)
        if peer is None:
            raise HTTPException(409, "分片完整原委托队列已变化")
        read_bytes += peer._read_text_bytes
        # A prior service CAS/consumption terminal is a stronger clock floor
        # than its earlier book timestamp or resource-entry watermark.
        if peer.filled_quantity:
            updated_leaf, invalid_updated = resources._bounded_leaf(TradeOrder.updated_at)
            updated = (await db.execute(select(updated_leaf, invalid_updated).where(
                TradeOrder.id == identifier))).one()
            proof = resources._object(peer.risk_json, reject_duplicate_keys=True)
            previous = proof.get("paper_after_hours_consumption")
            if (updated[1] or updated[0] is None or allocator._clock(updated[0]) > now
                    or not isinstance(previous, dict)
                    or allocator._clock(previous["terminal_checked_at"]) > now):
                raise HTTPException(409, "下一分片时钟早于原委托已存CAS或资源终验时钟")
            read_bytes += len(updated[0].isoformat().encode())
            if read_bytes > resources.MAX_READ_BYTES:
                raise HTTPException(409, "分片完整原委托时钟读取超出预算")
        receipts = await db.scalar(select(func.count()).select_from(resources.Receipt).where(
            resources.Receipt.order_id == peer.order_id))
        fills = await db.scalar(select(func.count()).select_from(TradeFill).where(
            TradeFill.order_id == peer.order_id))
        if (peer.broker != "paper" or receipts != counts.get(peer.order_id, 0)
                or fills != receipts or peer.filled_quantity != quantities.get(peer.order_id, 0)):
            raise HTTPException(409, "分片原委托存在缺失、额外或跨协议的经济回报")
        context.append(order if peer.id == order.id else peer)
    if set(quantities) - {peer.order_id for peer in context}:
        raise HTTPException(409, "分片回报缺少原委托，禁止忽略历史")
    return context


async def _execute_paper_after_hours_partial_order(db, order_id, *, feed):
    """Private next-fragment attempt, NOT a public retry/automatic execution API."""
    return await _execute_paper_after_hours_order(db, order_id, feed=feed, partial=True)


async def _execute_paper_after_hours_order(db, order_id, *, feed, partial=False):
    """Internal future-adapter boundary; NOT an API, scheduler or client certificate.

    Catalog-verified order-level input only. Current production catalog always
    waits. Default remains full quantity once; the explicit private partial entry
    freezes all originals and actual history for ONE fragment. No new accounts,
    five-depth, public matcher or automatic strategy execution.
    The original service scope spans real broker/book AND receipt/resource CAS.
    """
    from sqlalchemy import and_, update
    from app.api.v1 import paper
    from app.models.paper import PaperAccount
    from app.models.governance import TradeCalendarModel
    from app.trading import paper_after_hours_allocation as allocator
    from app.trading import paper_after_hours_resources as resources
    from app.trading.paper_after_hours_execution import (
        MODE, fifo_key, _freeze_fill_candidate, _freeze_partial_fill_candidate,
        _bounded_candidate_json, _partial_fee_parameters_match)
    from app.trading.paper_authorization import (_paper_execution_scope,
        _paper_partial_execution_scope, paper_order_checkpoint)

    if type(partial) is not bool:
        raise HTTPException(403, "盘后分片模式必须由私有服务显式选择")
    if db.new or db.dirty or db.deleted:
        raise HTTPException(409, "盘后撮合入口不得携带未归属的数据库变更")
    order = await db.scalar(select(TradeOrder).where(TradeOrder.order_id == order_id)
                            .execution_options(populate_existing=True))
    if order is None:
        raise HTTPException(404, "委托不存在")
    if (order.broker != "paper" or order.order_type != MODE
            or order.account_id not in paper.PAPER_ALL_ACCOUNTS
            or order.strategy_id or order.source not in {"", "manual"}):
        raise HTTPException(403, "盘后全笔核仅限已有常规账户的显式人工委托")
    fifo_key(order)
    now = allocator._clock(paper._public_order_clock())
    if order.status == "filled":
        roots = await resources._verified_receipt_bindings(db,
            account_numeric_id=json.loads(order.risk_json)["paper_after_hours_intent"]["numeric_account_id"],
            code=order.code, trade_date=order.trade_date, cutoff=now, partial=partial)
        if not any(binding["order_id"] == order.order_id for binding in roots.values()):
            raise HTTPException(409, "盘后终态回报不完整，禁止再次成交")
        return {"order": _order_payload(order), "fills": [_fill_payload(f) for f in await _order_fills(db, order_id)],
                "idempotent_replay": True}
    if partial:
        if (type(order.filled_quantity) is not int or order.filled_quantity % 100
                or not 0 <= order.filled_quantity < order.quantity
                or order.status != ("submitted" if order.filled_quantity == 0 else "partial")):
            raise HTTPException(409, "分片原委托状态或累计经济事实不一致")
    elif order.status != "submitted" or order.filled_quantity != 0:
        raise HTTPException(409, "盘后全笔v1不处理终态、部分成交或异常经济事实")
    # Freeze the frame before awaits; no caller can alter it while the lock waits.
    frozen_feed = json.loads(_bounded_candidate_json(feed))
    try:
        allocator._verify(frozen_feed, now=now)
    except allocator.EvidenceUnavailable as exc:
        return {"order": _order_payload(order), "status": "waiting",
                "reason": str(exc), "fills": [], "automatic_execution": False}

    fill_rows = []
    async with _paper_order_transaction(db, order=order):
        now = allocator._clock(paper._public_order_clock())
        snapshot = allocator._verify(frozen_feed, now=now)
        calendar = await db.get(TradeCalendarModel, order.trade_date, populate_existing=True)
        if (calendar is None or calendar.is_trade_day is not True or calendar.session_type != "full"
                or order.trade_date != now.date() or order.code != snapshot.code):
            raise HTTPException(409, "盘后撮合缺少当日完整交易会话资格")
        fifo_key(order)
        if not partial:
            heads = list((await db.scalars(select(TradeOrder.id).where(
                TradeOrder.account_id == order.account_id, TradeOrder.code == order.code,
                TradeOrder.trade_date == order.trade_date, TradeOrder.side == order.side,
                TradeOrder.broker == "paper", TradeOrder.order_type == MODE,
                TradeOrder.status.in_(("pending", "submitted", "partial")),
            ).order_by(TradeOrder.id).limit(allocator.MAX_LOCAL_ORDERS + 1))).all())
            if len(heads) > allocator.MAX_LOCAL_ORDERS:
                raise HTTPException(409, "本地盘后队列超出完整读取预算，禁止跳过前排")
            local_bytes = 0
            for head_id in heads:
                if head_id == order.id:
                    break
                # Only a fully checked, price-incompatible earlier intent is not a
                # legal queue entry. Unknown/corrupt/partial or underfunded heads block.
                earlier = await resources._book_row(db, TradeOrder, TradeOrder.id == head_id,
                    max_text_bytes=allocator.MAX_LOCAL_PROOF_BYTES - local_bytes)
                if earlier is None:
                    raise HTTPException(409, "本地盘后前排委托缺失，禁止跳过")
                local_bytes += earlier._read_text_bytes
                check = allocator.propose_allocations(frozen_feed, [earlier], now=now,
                    scenario_account=order.account_id)
                if (check["status"] != "waiting" or len(check["waiting"]) != 1
                        or check["waiting"][0]["reason"] != "original_limit_incompatible_with_official_close"):
                    return {"order": _order_payload(order), "status": "waiting",
                            "reason": "earlier_local_full_fill_intent", "fills": []}
            else:
                raise HTTPException(409, "本地盘后队列缺少原委托，禁止成交")
        accounts = list((await db.scalars(select(PaperAccount).where(
            PaperAccount.account_name == order.account_id, PaperAccount.status == "active")
            .limit(2).execution_options(populate_existing=True))).all())
        intent = json.loads(order.risk_json)["paper_after_hours_intent"]
        if (len(accounts) != 1 or type(intent.get("numeric_account_id")) is not int
                or accounts[0].id != intent["numeric_account_id"]):
            raise HTTPException(409, "盘后原活动账户身份已变化，禁止新建或重开账户")
        account = accounts[0]
        await resources._verify_sqlite_guards(db)
        key, _ = resources._scope_values(snapshot, account)
        state = await resources._book_row(db, resources.State, and_(
            resources.State.account_numeric_id == account.id,
            resources.State.code == order.code, resources.State.trade_date == order.trade_date))
        if state is not None:
            if state.scope_key != key:
                raise HTTPException(409, "盘后来源或session不能切换建立新资源池")
            await resources._verified_receipt_bindings(db, account_numeric_id=account.id,
                code=order.code, trade_date=order.trade_date, cutoff=now, partial=partial)
        priors = await resources._durable_priors(db, key, account, cutoff=now, partial=partial)
        if state is None and priors:
            raise HTTPException(409, "盘后历史消耗缺少根，禁止重建为零")
        context = await _after_hours_partial_context(db, order, priors, now=now) if partial else [order]
        propose = allocator.propose_partial_allocations if partial else allocator.propose_allocations
        planned = propose(frozen_feed, context, now=now,
            scenario_account=order.account_id, prior_proposals=priors)
        if partial and planned["proposals"]:
            first = next((p for p in planned["proposals"] if p["side"] == order.side), None)
            if first is None or first["order_id"] != order.order_id:
                return {"order": _order_payload(order), "status": "waiting",
                        "reason": "earlier_local_partial_intent", "fills": []}
        if not planned["proposals"]:
            return {"order": _order_payload(order), "status": planned["status"],
                    "reason": planned.get("reason"), "waiting": planned["waiting"], "fills": []}

        round_id = "after-hours-" + hashlib.sha256(
            _json_dumps([snapshot.source, snapshot.version, snapshot.session_id, snapshot.frame_id]).encode()).hexdigest()[:32]
        freeze = _freeze_partial_fill_candidate if partial else _freeze_fill_candidate
        options = {"local_orders": context} if partial else {}
        preview = freeze(frozen_feed, order, account_numeric_id=account.id,
            quote_round_id=round_id, dispatch_at=now, prior_proposals=priors, **options)
        execution = json.loads(preview.contract_json)
        risk = _json_loads_dict(order.risk_json)
        if not await _record_paper_account_integrity(db, order, execution, risk):
            if partial:
                raise HTTPException(409, "分片已有经济事实须保留，账务对账未通过")
            return {"order": _order_payload(order), "risk": risk, "fills": []}
        cmd = SubmitOrderCommand(code=order.code, side=order.side, price=order.price,
            quantity=order.quantity, account_id=order.account_id, order_type=MODE,
            strategy_version=order.strategy_version, signal_id=order.signal_id or "",
            source=order.source or "", reason=order.reason or "", decision_at=order.decision_at,
            decision_round_id=order.decision_round_id, after_hours_manual_intent=True)
        # Risk consumes the actual fragment quantity, not the whole original.
        # This adapter is risk-only; it never relabels the typed partial contract.
        risk_execution = ({**execution, "filled_quantity": execution["fragment_quantity"]}
                          if partial else execution)
        locked = await _locked_paper_risk_evidence(db, order, cmd, risk_execution,
            block_warn=True, initial_risk=risk)
        risk["paper_after_hours_fill_risk"] = locked
        order.risk_json = _json_dumps(risk)
        if locked["status"] != "validated":
            if partial:
                raise HTTPException(409, "分片锁后风控未通过，回滚本次而不覆盖既有部分成交")
            return {"order": _order_payload(order), "risk": risk, "fills": []}
        # Original account proof is rechecked by both locked risk and the book.
        dispatch = allocator._clock(paper._public_order_clock())
        if dispatch < allocator._clock(locked["completed_at"]):
            raise HTTPException(409, "盘后dispatch时钟早于锁后风控完成时钟")
        candidate = freeze(frozen_feed, order, account_numeric_id=account.id,
            quote_round_id=round_id, dispatch_at=dispatch, prior_proposals=priors, **options)
        contract = resources._partial_resource_contract(candidate) if partial else json.loads(candidate.contract_json)
        quantity = contract["fragment_quantity"] if partial else order.quantity
        req = BrokerOrderRequest(order_id=contract["request_id"], code=order.code, side=order.side,
            price=contract["fill_price"], quantity=quantity, order_type=MODE,
            account_name=order.account_id, strategy_version=order.strategy_version,
            signal_id=order.signal_id or order.order_id, source=order.source or "", reason=order.reason or "",
            decision_round_id=order.decision_round_id, fill_round_id=round_id, filled_at=dispatch)
        await db.flush()
        ready = dict(paper_order_checkpoint(order))
        # Do NOT use _dispatch_for_service: it closes the scope before receipt, and
        # wrapping finalize/CAS failures as broker rejection would corrupt recovery.
        broker = get_broker_adapter("paper")
        scope = (_paper_partial_execution_scope(db, req, candidate=candidate) if partial else
                 _paper_execution_scope(db, req, immediate_evidence_json=candidate.contract_json,
                                        fixed_candidate=candidate))
        with scope:
            result = await broker.place_order(db, req)
            if (result.accepted is not True or result.status != "filled" or len(result.fills) != 1):
                raise HTTPException(409, "盘后book没有唯一真实全笔回报；原事务必须回滚")
            actual = result.fills[0]
            row = TradeFill(fill_id=actual.fill_id, order_id=order.order_id, broker="paper",
                external_order_id=result.external_order_id, code=order.code, side=order.side,
                price=actual.price, quantity=actual.quantity, commission=actual.commission,
                tax=actual.tax, realized_pnl=actual.realized_pnl, broker_trade_id=actual.broker_trade_id,
                raw_json=_json_dumps({**actual.raw, "after_hours_fixed_execution": contract}),
                decision_round_id=order.decision_round_id, fill_round_id=round_id,
                trade_date=order.trade_date, filled_at=actual.filled_at)
            db.add(row)
            await db.flush()
            finalize_options = {"partial_candidate": candidate} if partial else {}
            consumption = await resources._persist_fill_resources(db,
                order_id=order.order_id, fill_id=row.fill_id, feed=frozen_feed, **finalize_options)
            risk["paper_after_hours_consumption"] = consumption
            terminal = allocator._clock(consumption["terminal_checked_at"])
            cas_at = allocator._clock(paper._public_order_clock())
            if cas_at < terminal:
                raise HTTPException(409, "盘后委托CAS时钟早于资源终验时钟")
            allocator._verify(frozen_feed, now=cas_at)
            cumulative = ready["filled_quantity"] + actual.quantity if partial else actual.quantity
            if partial and cumulative != contract["cumulative_after"]:
                raise HTTPException(409, "分片实际成交与原委托累计坐标不一致")
            changed = await db.execute(update(TradeOrder).where(*(
                getattr(TradeOrder, name) == value for name, value in ready.items()
            )).values(status="filled" if cumulative == order.quantity else "partial",
                filled_quantity=cumulative, avg_fill_price=actual.price,
                last_fill_round_id=round_id, external_order_id=result.external_order_id,
                error_message=None, risk_json=_json_dumps(risk), updated_at=cas_at)
                .execution_options(synchronize_session=False))
            if changed.rowcount != 1:
                raise HTTPException(409, "盘后委托终态CAS冲突，禁止保留局部成交或释放消耗")
            await db.refresh(order)
            completed = allocator._clock(paper._public_order_clock())
            if completed < cas_at:
                raise HTTPException(409, "盘后服务终验时钟早于委托CAS时钟")
            allocator._verify(frozen_feed, now=completed)
            # Replaying a large frame is synchronous but still consumes real time.
            # Sample AFTER that last replay; no await/replay follows this terminal check.
            terminal = allocator._clock(paper._public_order_clock())
            if (terminal < completed or terminal >= snapshot.expires_at
                    or partial and not _partial_fee_parameters_match(contract["fee_preview"])):
                raise HTTPException(409, "盘后服务同步终验后时钟回退、费用漂移或原来源/会话已到期")
            fill_rows.append(row)
    await db.refresh(order)
    for row in fill_rows:
        await db.refresh(row)
    return {"order": _order_payload(order), "risk": risk,
            "fills": [_fill_payload(row) for row in fill_rows], "automatic_execution": False}


async def reconcile_paper_after_hours_intents(db, *, limit=500):
    """Expire verified zero/partial remainders; no broker, scan or resource release."""
    from app.api.v1 import paper
    from app.trading.paper_after_hours_execution import MODE, waiting_plan
    now = paper._public_order_clock()
    if type(limit) is not int or not 1 <= limit <= 500:
        raise ValueError("fixed-price expiry limit must be 1..500")
    # Freeze scalars: one failed cancellation rolls back/EXPIRES every ORM object.
    rows = list((await db.execute(select(TradeOrder.id, TradeOrder.order_id, TradeOrder.trade_date).where(
        TradeOrder.order_type == MODE, TradeOrder.broker == "paper",
        TradeOrder.status.in_(("submitted", "partial")), TradeOrder.trade_date <= now.date(),
    ).order_by(TradeOrder.trade_date, TradeOrder.id).limit(limit + 1))).all())
    canceled, errors, active_ids = [], [], []
    for identifier, order_id, trade_day in rows[:limit]:
        if now >= datetime.combine(trade_day, time(15, 30)):
            try:
                order = await db.get(TradeOrder, identifier, populate_existing=True)
                if order is None:
                    continue
                result = await _cancel_paper_after_hours_intent(db, order, reason_kind="session_end")
                if result["status"] == "canceled":
                    canceled.append(order_id)
            except HTTPException as exc:
                errors.append({"order_id": order_id, "http_status": exc.status_code})
        else:
            active_ids.append(identifier)
    active = list((await db.scalars(select(TradeOrder).where(
        TradeOrder.id.in_(active_ids), TradeOrder.status.in_(("submitted", "partial"))
    ).execution_options(populate_existing=True))).all()) if active_ids else []
    from app.trading.paper_after_hours_execution import fifo_key
    validated_active = []
    for order in active:
        try:
            fifo_key(order)
            from app.trading.paper_after_hours_allocation import _integer
            filled = _integer(order.filled_quantity, zero=True)
            if (_integer(order.quantity) % 100 or filled % 100 or not filled < order.quantity
                    or order.status != ("partial" if filled else "submitted")):
                raise ValueError("invalid_active_remainder")
            validated_active.append(order)
        except (ValueError, KeyError, TypeError, AttributeError, RecursionError, OverflowError):
            errors.append({"order_id": order.order_id, "http_status": 409})
    from app.trading.paper_after_hours_allocation import matching_capabilities
    return {"status": "partial" if errors else "observed", "canceled": canceled, "errors": errors,
            "waiting": waiting_plan(validated_active), "truncated": len(rows) > limit, "fills": [],
            "matching_capability": "aggregate_only", "source_capabilities": matching_capabilities(),
            "automatic_execution": False}


async def cancel_order(db: AsyncSession, order_id: str) -> dict:
    order = (
        await db.execute(select(TradeOrder).where(TradeOrder.order_id == order_id))
    ).scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="委托不存在")
    if order.order_type == "after_hours_fixed":
        return await _cancel_paper_after_hours_intent(db, order, reason_kind="user_cancel")
    if order.status in {"filled", "canceled", "risk_blocked", "rejected"}:
        return {"order": _order_payload(order), "status": "unchanged", "reason": "当前状态不可撤单"}

    if (
        order.broker == "paper"
        and order.status == "submitted"
        and str(order.external_order_id or "").startswith("paper-queue-")
    ):
        order.status = "canceled"
        order.error_message = "用户撤销涨停排队委托"
        await db.commit()
        await db.refresh(order)
        return {
            "order": _order_payload(order),
            "status": "canceled",
            "reason": order.error_message,
        }

    broker = get_broker_adapter(order.broker)
    result = await broker.cancel_order(db, order.external_order_id or order.order_id)
    if result.status == "canceled":
        order.status = "canceled"
    else:
        order.error_message = result.error_message or "撤单失败"
    await db.commit()
    await db.refresh(order)
    return {"order": _order_payload(order), "status": result.status, "reason": result.error_message}


async def sync_broker(db: AsyncSession, broker_name: str = "paper") -> dict:
    broker = get_broker_adapter(broker_name)
    account_payload = await broker.sync_account(db)
    positions_payload = await broker.sync_positions(db)
    db.add(BrokerSyncSnapshot(
        broker=broker_name,
        account_id="default",
        snapshot_type="account",
        payload_json=_json_dumps(account_payload),
        status="ok",
    ))
    db.add(BrokerSyncSnapshot(
        broker=broker_name,
        account_id="default",
        snapshot_type="positions",
        payload_json=_json_dumps(positions_payload),
        status="ok",
    ))
    await db.commit()
    return {"broker": broker_name, "account": account_payload, "positions": positions_payload}


async def list_orders(db: AsyncSession, limit: int = 100, status: Optional[str] = None) -> dict:
    query = select(TradeOrder).order_by(desc(TradeOrder.created_at)).limit(limit)
    if status:
        query = select(TradeOrder).where(TradeOrder.status == status).order_by(desc(TradeOrder.created_at)).limit(limit)
    rows = (await db.execute(query)).scalars().all()
    return {"orders": [_order_payload(row) for row in rows]}


async def list_fills(db: AsyncSession, limit: int = 100) -> dict:
    rows = (
        await db.execute(select(TradeFill).order_by(desc(TradeFill.filled_at)).limit(limit))
    ).scalars().all()
    return {"fills": [_fill_payload(row) for row in rows]}
