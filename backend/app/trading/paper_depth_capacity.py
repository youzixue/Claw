"""Receipt-backed visible depth guard for ONE independent simulated account.

No production order routing, shared Champion/Challenger portfolio, new fill-price
model, cross-process mutex, or exchange FIFO is introduced. Check inside the
existing paper transaction; its receipt commit is the only capacity consumption.
"""
import hashlib
import math

from fastapi import HTTPException
from sqlalchemy import and_, or_, select

from app.models.trading import TradeFill, TradeOrder
from app.trading.paper_authorization import paper_transaction_active


def visible_depth_capacity(spot, *, side):
    """Reuse the existing participation/lot arithmetic, without an order's limit."""
    from app.trading.service import _depth_fill_plan, _to_float, settings

    prefix = "ask" if side == "buy" else "bid"
    prices = [_to_float(getattr(spot, f"{prefix}{i}_price", None)) for i in range(1, 6)]
    prices = [price for price in prices if price is not None and price > 0]
    ratio = _to_float(settings.PAPER_DEPTH_MAX_PARTICIPATION_RATIO)
    if side not in ("buy", "sell") or not prices or ratio is None:
        return {"participation_ratio": None, "levels": []}
    _, _, levels = _depth_fill_plan(spot, side=side,
        limit_price=max(prices) if side == "buy" else min(prices),
        remaining_quantity=2**63-1)
    return {"participation_ratio": max(0.0, min(1.0, ratio)), "levels": levels}


def _levels(rows):
    if not isinstance(rows, list) or not rows:
        raise ValueError("missing_depth")
    result = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("invalid_depth")
        level, price, quantity = (row.get(key) for key in ("level", "price", "quantity"))
        if (type(level) is not int or not 1 <= level <= 5 or level in result
                or type(price) not in (int, float) or not math.isfinite(price) or price <= 0
                or type(quantity) is not int or quantity < 100 or quantity % 100):
            raise ValueError("invalid_depth")
        result[level] = (price, quantity)
    return result


async def account_round_depth_evidence(db, order, execution):
    """Validate the already-risk-checked plan, never silently reprice or upsize it.

    A conflicting plan waits/rejects even if another unplanned level has room.
    No reservation is written: only durable receipts are counted, so rollback
    consumes nothing. Legacy/unqualified same-scope receipts fail closed.
    """
    if not paper_transaction_active(db):
        raise HTTPException(403, "共享模拟容量验收必须在原子成交事务内")
    from app.trading.service import _json_dumps, _json_loads_dict

    proof = {"contract_version": "paper_account_round_depth_v1_20260914",
        "scope": "independent_account_round_code_side", "status": "unavailable",
        "account_id": str(order.account_id), "order_id": order.order_id,
        "code": order.code, "side": order.side,
        "quote_round_id": execution.get("quote_round_id"),
        "prior_fill_count": 0, "simulation_only": True}
    def rejected(reason):
        proof["reason"] = "同账户轮次容量未通过：" + reason
        return proof
    try:
        round_id = execution.get("quote_round_id")
        if not round_id:
            return rejected("缺少实际行情轮次")
        snapshot = execution.get("visible_depth_capacity")
        capacity = _levels(snapshot.get("levels") if isinstance(snapshot, dict) else None)
        planned_rows = (execution.get("queue_open_depth") or execution).get("depth_levels")
        planned = _levels(planned_rows)
        requested = execution.get("filled_quantity")
        if type(requested) is not int or sum(q for _, q in planned.values()) != requested:
            return rejected("计划量与冻结合同不符")
        for level, (price, quantity) in planned.items():
            if level not in capacity or capacity[level][0] != price or quantity > capacity[level][1]:
                return rejected("计划档位不属于原验收盘口容量")
        fingerprint = hashlib.sha256(_json_dumps({
            "capacity": snapshot,
            **{key: execution.get(key) for key in (
                "source_quote_at", "received_at", "quote_committed_at")},
        }).encode()).hexdigest()
        proof.update(snapshot_sha256=fingerprint, requested_quantity=requested)
        used = {level: 0 for level in capacity}
        with db.no_autoflush:
            rows = (await db.execute(select(
                TradeFill.fill_id, TradeFill.order_id, TradeFill.code, TradeFill.side,
                TradeFill.price, TradeFill.quantity, TradeFill.raw_json, TradeFill.filled_at,
                TradeOrder.account_id, TradeOrder.code, TradeOrder.side, TradeOrder.broker,
                TradeFill.fill_round_id, TradeFill.broker, TradeOrder.last_fill_round_id,
            ).outerjoin(TradeOrder, TradeOrder.order_id == TradeFill.order_id).where(or_(
                # Do not filter ONLY by the mutable receipt identity being checked.
                and_(TradeOrder.account_id == str(order.account_id),
                     TradeOrder.code == order.code, TradeOrder.trade_date == order.trade_date),
                and_(TradeFill.fill_round_id == round_id, TradeFill.code == order.code),
            )).order_by(TradeFill.id))).all()
        for (fill_id, prior_order_id, code, side, price, quantity, raw_json, filled_at,
             account_id, order_code, order_side, order_broker, receipt_round, receipt_broker,
             order_last_round) in rows:
            raw = _json_loads_dict(raw_json)
            prior = raw.get("immediate_execution_evidence") or raw.get("pending_execution_timing")
            ledger = raw.get("ledger_execution_timing")
            bound_rounds = {receipt_round,
                prior.get("quote_round_id") if isinstance(prior, dict) else None,
                ledger.get("quote_round_id") if isinstance(ledger, dict) else None} - {None, ""}
            if round_id not in bound_rounds and (bound_rounds or order_last_round != round_id):
                continue  # A different, consistent historical round consumes its own pool.
            bound_sides = {side, order_side, prior.get("side") if isinstance(prior, dict) else None}
            if order.side not in bound_sides:
                continue  # Buy/ask and sell/bid capacity are independent.
            if account_id is None:
                return rejected("同轮回报缺少账户归属；不猜测历史容量")
            if account_id != str(order.account_id):
                if isinstance(prior, dict) and prior.get("account_id") == str(order.account_id):
                    return rejected("旧委托账户与原成交合同归属冲突")
                continue  # Independent counterfactual account, NOT a shared portfolio.
            ledger = raw.get("ledger_execution_timing")
            if not isinstance(prior, dict) or not isinstance(ledger, dict):
                return rejected("同轮旧回报缺少冻结容量证据")
            prior_cap = prior.get("account_round_depth")
            prior_kind = prior.get("contract_version")
            immediate = prior_kind == "immediate_paper_fill_v2_20260914"
            if (prior_kind not in ("immediate_paper_fill_v2_20260914", "pending_paper_fill_timing_v1_20260914")
                    or prior.get("status") != ("fillable" if immediate else "validated")
                    or prior.get("mandatory") is not True or not isinstance(prior_cap, dict)
                    or prior_cap.get("contract_version") != proof["contract_version"]
                    or prior_cap.get("scope") != proof["scope"] or prior_cap.get("status") != "validated"
                    or prior_cap.get("snapshot_sha256") != fingerprint
                    or prior_cap.get("order_id") != prior_order_id
                    or prior_cap.get("account_id") != account_id
                    or prior_cap.get("code") != code or prior_cap.get("side") != side
                    or prior_cap.get("quote_round_id") != round_id
                    or prior_cap.get("requested_quantity") != quantity
                    or prior.get("code") != code or prior.get("side") != side
                    or prior.get("account_id") != account_id or prior.get("quote_round_id") != round_id
                    or prior.get("fill_price") != price or prior.get("filled_quantity") != quantity
                    or fill_id != "fill-" + str(prior_order_id if immediate else prior.get("request_id"))
                    or order_code != code or order_side != side or order_broker != "paper"
                    or receipt_round != round_id or receipt_broker != "paper"
                    or ledger.get("status") != "validated"
                    or ledger.get("guard_version") != "paper_ledger_timing_v1_20260914"
                    or ledger.get("input_contract_version") != prior_kind
                    or ledger.get("quote_round_id") != round_id
                    or ledger.get("before_mutation_checked_at") != filled_at.isoformat()
                    or ledger.get("input_sha256") != hashlib.sha256(_json_dumps(prior).encode()).hexdigest()):
                return rejected("同轮旧回报身份/合同/SHA不符或盘口容量被改写")
            allocated = _levels((prior.get("queue_open_depth") or prior).get("depth_levels"))
            if sum(q for _, q in allocated.values()) != quantity:
                return rejected("既有回报量与原档位分配不符")
            for level, (level_price, amount) in allocated.items():
                if level not in capacity or capacity[level][0] != level_price:
                    return rejected("同轮已消费档位与原盘口不符")
                used[level] += amount
                if used[level] > capacity[level][1]:
                    return rejected("既有同轮分配已超容量，不能修补旧回报")
            proof["prior_fill_count"] += 1
        proof["levels"] = [{"level": level, "price": price, "capacity_quantity": available,
            "consumed_quantity": used[level], "planned_quantity": planned.get(level, (0, 0))[1]}
            for level, (price, available) in capacity.items()]
        if any(used[level] + amount > capacity[level][1] for level, (_, amount) in planned.items()):
            return rejected("本次计划档位已被同账户其它成交消费；等待新撮合，不复用旧容量")
        proof.update(status="validated", reason="原验收档位剩余容量覆盖本次计划；其它策略账户独立")
        return proof
    except (TypeError, ValueError, OverflowError, AttributeError):
        return rejected("冻结容量或既有回报缺失/非法，保留原账本等待核对")
