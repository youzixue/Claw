"""Mandatory full-fill-or-reject evidence for all immediate paper orders.

Module/function names retained for compatibility with the original public boundary.

The order price is a limit, never a synthetic execution price. No queue, auction,
partial-fill remainder, future quote or network-calendar fallback is invented.
"""
from datetime import datetime, time, timedelta

from app.core.trade_calendar import is_official_closed_day
from sqlalchemy import select
from app.models.governance import TradeCalendarModel
from app.models.stock import QuoteRound


def depth_quote_rejection_reason(spot, *, limit_price):
    """Shared immediate/open-queue validation of the supplied five-level book."""
    from app.trading.service import _to_float

    down, up, last = (_to_float(getattr(spot, key, None))
                      for key in ("limit_down", "limit_up", "price"))
    if (down is None or up is None or last is None
            or not 0 < down < up or not down <= last <= up
            or not down <= limit_price <= up):
        return "涨跌停价格边界缺失/冲突或委托价越界"
    # Reject contradictory positive levels rather than silently excluding bad data.
    for prefix in ("ask", "bid"):
        previous = None
        for level in range(1, 6):
            raw_price = getattr(spot, f"{prefix}{level}_price", None)
            raw_hands = getattr(spot, f"{prefix}{level}_volume", None)
            price, hands = _to_float(raw_price), _to_float(raw_hands)
            if ((raw_price is not None and price is None)
                    or (raw_hands is not None and hands is None)):
                return "盘口含非有限或非法值"
            if price is None and hands is None:
                continue
            if price == 0 and (hands is None or hands == 0):
                continue
            if (price is None or hands is None or hands < 0 or price <= 0
                    or not down <= price <= up):
                return "盘口档位缺失、非有限或越涨跌停边界"
            if previous is not None and ((prefix == "ask" and price <= previous)
                                        or (prefix == "bid" and price >= previous)):
                return "盘口档位价格重复或乱序"
            previous = price
    ask = _to_float(getattr(spot, "ask1_price", None))
    bid = _to_float(getattr(spot, "bid1_price", None))
    if ask and bid and bid >= ask:
        return "买卖盘口交叉或锁定，不能证明连续竞价可成交"
    return None


def queue_open_fill_evidence(spot, *, limit_price, quantity):
    """Full visible quantity only; keep the original pessimistic queue limit.

    Not a shared-capacity reservation or proof of exchange queue priority.
    Timing/round identity are separately frozen by pending_fill_timing_evidence.
    """
    from app.api.v1 import paper
    from app.trading.service import _depth_fill_plan, _to_float

    ratio = _to_float(paper.settings.PAPER_DEPTH_MAX_PARTICIPATION_RATIO)
    proof = {"contract_version": "queue_open_depth_v1_20260914",
        "scope": "one_order_visible_depth_not_shared_capacity",
        "status": "waiting", "requested_quantity": quantity, "limit_price": limit_price,
        "partial_fill_allowed": False, "simulation_only": True,
        "participation_ratio": max(0.0, min(1.0, ratio)) if ratio is not None else None,
        "depth_levels": [], "visible_fillable_quantity": 0}
    reason = depth_quote_rejection_reason(spot, limit_price=limit_price)
    if reason:
        proof["reason"] = reason
        return proof
    filled, vwap, levels = _depth_fill_plan(
        spot, side="buy", limit_price=limit_price, remaining_quantity=quantity)
    proof.update(visible_fillable_quantity=filled, depth_vwap=vwap, depth_levels=levels)
    executable = paper._conservative_execution_price(spot, "buy")
    if (type(quantity) is not int or quantity < 100 or quantity % 100
            or filled != quantity or vwap is None or executable is None
            or executable > limit_price + 1e-9):
        proof["reason"] = "开板后原限价五档深度不足或价格不可执行；整笔继续等待，不以现价补量"
        return proof
    proof.update(status="fillable", fill_price=limit_price,
        reason="开板后原限价五档量覆盖整笔；仍按原涨停限价保守记账")
    return proof


async def public_fill_evidence(db, cmd, *, now, validation_clock=None):
    from app.api.v1 import paper
    from app.trading.service import _depth_fill_plan, _paper_execution_spot, _to_float

    evidence = {"contract_version": "immediate_paper_fill_v2_20260914",
                "scope": "all_immediate_paper_orders", "mandatory": True,
                "code": cmd.code, "side": cmd.side, "account_id": str(cmd.account_id),
                "requested_decision_round_id": cmd.decision_round_id,
                "status": "rejected",
                "evaluated_at": now.isoformat() if isinstance(now, datetime) else None,
                "requested_quantity": cmd.quantity, "limit_price": cmd.price,
                "simulation_only": True, "partial_fill_allowed": False}
    def rejected(reason):
        evidence["reason"] = reason
        return evidence

    if cmd.order_type != "limit":
        return rejected("即时模拟成交仅支持明确限价委托，不把市价单改当自填成交价")
    cutoff = cmd.decision_at or now
    if (not isinstance(now, datetime) or now.tzinfo is not None or not isinstance(cutoff, datetime)
            or cutoff.tzinfo is not None or cutoff > now or cutoff.date() != now.date()):
        return rejected("决策/验收时钟必须同交易日且未回拨的本地时间")
    evidence["decision_at"] = cutoff.isoformat()
    if now.weekday() >= 5 or is_official_closed_day(now.date()):
        return rejected("休市日不接受即时模拟成交")
    with db.no_autoflush:
        calendar = await db.get(TradeCalendarModel, now.date())
    if (calendar is None or calendar.is_trade_day is not True
            or calendar.session_type != "full"):
        return rejected("缺少本地已确认完整交易日日历，禁止按工作日或网络回退成交")
    if not (time(9, 30) <= now.time() < time(11, 30)
            or time(13) <= now.time() < time(14, 57)):
        return rejected("当前不在连续竞价时段；集合竞价/午休/盘后不能模拟即时成交")
    spot = await _paper_execution_spot(db, cmd.code)
    if spot is None:
        return rejected("缺少真实行情")
    if str(getattr(spot, "code", "")) != cmd.code:
        return rejected("行情证券身份与委托不一致")
    round_id = str(getattr(spot, "quote_round_id", "") or "")
    if not round_id:
        return rejected("缺少行情轮次身份")
    evidence["quote_round_id"] = round_id
    if cmd.decision_round_id and cmd.decision_round_id != round_id:
        return rejected("委托决策轮次与实际行情轮次不一致，禁止借用另一轮即时成交")
    clocks = [getattr(spot, key, None)
              for key in ("source_quote_at", "received_at", "updated_at")]
    if any(not isinstance(at, datetime) or at.tzinfo is not None for at in clocks):
        return rejected("缺少合法的源/接收/提交三时钟")
    if not clocks[0] <= clocks[1] <= clocks[2] <= cutoff:
        return rejected("行情三时钟乱序或未来数据，不允许模拟成交")
    with db.no_autoflush:
        round_row = await db.scalar(select(QuoteRound).where(QuoteRound.round_id == round_id))
    if (round_row is None or round_row.quality_status != "ok"
            or round_row.source != "tencent" or round_row.trade_date != now.date()
            or round_row.committed_at != clocks[2]
            or not isinstance(round_row.as_of_at, datetime)
            or round_row.as_of_at.tzinfo is not None
            or round_row.as_of_at.date() != now.date()
            or not round_row.as_of_at <= round_row.committed_at <= now):
        return rejected("行情轮次未确认健康、时钟不符或非已验收来源")
    if cmd.as_of_at is not None and cmd.as_of_at != round_row.as_of_at:
        return rejected("委托原始as_of与行情轮次证据不一致，禁止重贴决策依据")
    evidence.update({"quote_config_version": round_row.config_version,
                     "quote_code_version": round_row.code_version,
                     "quote_as_of_at": round_row.as_of_at.isoformat()})
    # DB awaits can span expiry/lunch/closing. Sample the real clock again after
    # the final query; no context committed_at is substituted for dispatch time.
    validation_at = validation_clock() if validation_clock is not None else now
    if (not isinstance(validation_at, datetime) or validation_at.tzinfo is not None
            or validation_at < now or validation_at.date() != now.date()
            or not (time(9, 30) <= validation_at.time() < time(11, 30)
                    or time(13) <= validation_at.time() < time(14, 57))):
        return rejected("查询后验收时钟回拨、跨日或已离开连续竞价，禁止即时成交")
    evidence["dispatch_validated_at"] = validation_at.isoformat()
    ok, reason = paper._execution_quote_status(spot, now.date(), now=validation_at)
    if not ok:
        return rejected(reason)
    reason = depth_quote_rejection_reason(spot, limit_price=cmd.price)
    if reason:
        return rejected(reason)
    quantity, price, levels = _depth_fill_plan(
        spot, side=cmd.side, limit_price=cmd.price, remaining_quantity=cmd.quantity)
    evidence.update({"quote_round_id": round_id,
                     "source_quote_at": clocks[0].isoformat(),
                     "received_at": clocks[1].isoformat(),
                     "quote_committed_at": clocks[2].isoformat(),
                     "depth_levels": levels, "visible_fillable_quantity": quantity})
    if quantity != cmd.quantity or price is None:
        return rejected("原限价五档可见深度不足；不按自填价格成交、不自动排队或部分成交")
    from app.trading.paper_depth_capacity import visible_depth_capacity
    evidence["visible_depth_capacity"] = visible_depth_capacity(spot, side=cmd.side)
    max_age = max(1, int(getattr(paper.settings, "PAPER_EXECUTION_QUOTE_MAX_AGE_SEC", 90)))
    session_end = time(11, 30) if validation_at.time() < time(11, 30) else time(14, 57)
    evidence.update({"status": "fillable", "fill_price": price,
                     "quote_max_age_sec": max_age,
                     "quote_expires_at": (clocks[0] + timedelta(seconds=max_age)).isoformat(),
                     "session_end_at": datetime.combine(validation_at.date(), session_end).isoformat(),
                     "filled_quantity": quantity, "reason": "真实限价内五档深度覆盖整笔模拟委托"})
    return evidence


async def _pending_round_progress(db, order, *, round_id, source, received, committed,
                                  evaluation, fill_quantity):
    """Only this order's consumed fills; no shared depth or cross-process certificate."""
    import hashlib
    import json
    from app.trading.service import _order_fills, _json_dumps
    from app.trading.paper_authorization import _local_clock

    result = {"schema": "per_order_fill_progress_v1_20260914",
              "scope": "consumed_fills_of_this_order_not_shared_capacity", "status": "rejected"}
    def rejected(reason):
        result["reason"] = reason
        return result
    # Read prior recorded evidence without flushing or repairing historical rows.
    with db.no_autoflush:
        fills = await _order_fills(db, order.order_id)
    seen, quantity = set(), 0
    previous_source = previous_received = previous_commit = previous_fill = None
    try:
        for row in fills:
            if not row.fill_round_id or row.fill_round_id in seen or row.fill_round_id == round_id:
                return rejected("待单已消费的轮次不得再次撮合，也不能沿用重复历史轮次")
            seen.add(row.fill_round_id)
            raw = json.loads(row.raw_json or "null")
            p = raw.get("pending_execution_timing") if isinstance(raw, dict) else None
            timing = raw.get("ledger_execution_timing") if isinstance(raw, dict) else None
            if (not isinstance(p, dict) or not isinstance(timing, dict)
                    or p.get("contract_version") != "pending_paper_fill_timing_v1_20260914"
                    or p.get("status") != "validated" or p.get("mandatory") is not True
                    or timing.get("status") != "validated"
                    or timing.get("guard_version") != "paper_ledger_timing_v1_20260914"
                    or timing.get("input_contract_version") != p["contract_version"]
                    or timing.get("input_sha256") != hashlib.sha256(_json_dumps(p).encode()).hexdigest()):
                return rejected("待单历史成交轮次的冻结合同/账本验收缺失或不一致，保留旧账等待审计")
            if (row.broker != "paper" or row.code != order.code or row.side != order.side
                    or row.trade_date != order.trade_date
                    or row.decision_round_id != order.decision_round_id
                    or row.fill_id != "fill-" + str(p.get("request_id") or "")
                    or p.get("order_id") != order.order_id or p.get("account_id") != order.account_id
                    or p.get("code") != row.code or p.get("side") != row.side
                    or p.get("decision_round_id") != order.decision_round_id
                    or p.get("quote_round_id") != row.fill_round_id
                    or p.get("fill_price") != row.price
                    or type(p.get("filled_quantity")) is not int or p["filled_quantity"] != row.quantity
                    or type(row.quantity) is not int or row.quantity < 100 or row.quantity % 100
                    or _local_clock(p.get("decision_at")) != order.decision_at
                    or _local_clock(timing.get("before_mutation_checked_at")) != row.filled_at
                    or timing.get("quote_round_id") != row.fill_round_id):
                return rejected("待单历史轮次的回报身份/价量/原决策或逻辑成交钟不一致")
            old_source, old_received, old_commit = [
                _local_clock(p.get(key)) for key in ("source_quote_at", "received_at", "quote_committed_at")]
            old_fill = _local_clock(row.filled_at)
            if (not old_source <= old_received <= old_commit <= old_fill <= evaluation
                    or any(at.date() != order.trade_date for at in (old_source, old_received, old_commit, old_fill))
                    or (previous_source is not None and not (
                        previous_source < old_source and previous_received < old_received
                        and previous_commit < old_commit and previous_fill <= old_fill))):
                return rejected("待单历史轮次时钟乱序或当前观察早于已成交证据")
            if not (old_source < source and old_received < received and old_commit < committed):
                return rejected("待单当前轮次源/接收/提交时钟未严格推进，不重复消费旧盘口")
            previous_source, previous_received, previous_commit, previous_fill = (
                old_source, old_received, old_commit, old_fill)
            quantity += row.quantity
        if (type(order.filled_quantity) is not int or quantity != order.filled_quantity
                or (fills and order.last_fill_round_id != fills[-1].fill_round_id)
                or (not fills and order.last_fill_round_id)
                or quantity + fill_quantity > order.quantity):
            return rejected("待单累计成交与已验收轮次回报不一致，不推断缺失历史")
        result.update({"status": "validated", "prior_fill_count": len(fills),
            "prior_filled_quantity": quantity,
            "last_fill_id": fills[-1].fill_id if fills else None,
            "last_fill_round_id": fills[-1].fill_round_id if fills else None,
            "last_source_quote_at": previous_source.isoformat() if fills else None,
            "last_received_at": previous_received.isoformat() if fills else None,
            "last_committed_at": previous_commit.isoformat() if fills else None,
            "last_filled_at": previous_fill.isoformat() if fills else None})
        return result
    except (ValueError, TypeError, OverflowError):
        return rejected("待单历史轮次时钟/合同非法，不以新报价补造原成交证据")


async def pending_fill_timing_evidence(db, order, metadata, spot, *, now,
        fill_round_id, fill_price, fill_quantity, request_id, queue_cancel_at=None):
    """Freeze timing AFTER original route/depth-or-queue/risk preflight.

    This is not full-depth/FIFO, latest identity, or shared-capacity certification.
    Original decision evidence remains distinct from the later matching frame.
    """
    from app.api.v1 import paper
    from app.trading.service import _pending_buy_time_reason, _requires_pending_buy_validity
    from app.trading.paper_authorization import _local_clock

    queued = queue_cancel_at is not None
    evidence = {"contract_version": "pending_paper_fill_timing_v1_20260914",
        "scope": "accepted_pending_quote_timing_only", "mandatory": True,
        "execution_kind": "limit_up_queue" if queued else "deferred",
        "code": order.code, "side": order.side, "account_id": str(order.account_id),
        "request_id": request_id, "order_id": order.order_id,
        "decision_round_id": order.decision_round_id, "quote_round_id": fill_round_id,
        "fill_price": fill_price, "filled_quantity": fill_quantity, "status": "rejected",
        "simulation_only": True}
    def rejected(reason):
        evidence["reason"] = reason
        return evidence

    try:
        evaluation = _local_clock(now)
        decision = _local_clock(order.decision_at)
        source, received, committed = [
            _local_clock(getattr(spot, key, None))
            for key in ("source_quote_at", "received_at", "updated_at")]
        context = paper._quote_round_context()
        if (str(getattr(spot, "code", "")) != order.code
                or not fill_round_id or not order.decision_round_id
                or fill_round_id == order.decision_round_id
                or fill_round_id != context.get("round_id")
                or fill_round_id != getattr(spot, "quote_round_id", None)
                or context.get("quality_status") != "ok"
                or context.get("committed_at") != committed):
            return rejected("待单撮合缺少或混用了实际行情/原决策轮次身份")
        if (not source <= received <= committed <= evaluation
                or not decision < committed
                or order.trade_date != evaluation.date()
                or any(at.date() != evaluation.date() for at in (source, received, committed, decision))):
            return rejected("待单三钟乱序/未来/跨日或行情提交不晚于原决策")
        if evaluation.weekday() >= 5 or is_official_closed_day(evaluation.date()):
            return rejected("待单撮合日期为休市日")
        if not (time(9, 30) <= evaluation.time() < time(11, 30)
                or time(13) <= evaluation.time() < time(14, 57)):
            return rejected("待单原撮合观察不在连续竞价时段")
        end = datetime.combine(evaluation.date(),
            time(11, 30) if evaluation.time() < time(11, 30) else time(14, 57))
        with db.no_autoflush:
            calendar = await db.get(TradeCalendarModel, evaluation.date())
            row = await db.scalar(select(QuoteRound).where(QuoteRound.round_id == fill_round_id))
        if (calendar is None or calendar.is_trade_day is not True or calendar.session_type != "full"):
            return rejected("待单缺少本地已确认完整交易日日历，不按工作日/网络回退")
        if (row is None or row.quality_status != "ok" or row.source != "tencent"
                or row.trade_date != evaluation.date() or row.committed_at != committed
                or _local_clock(row.as_of_at).date() != evaluation.date()
                or not source <= row.as_of_at <= committed
                or context.get("as_of_at") != row.as_of_at
                or context.get("config_version") != row.config_version
                or context.get("code_version") != row.code_version):
            return rejected("待单实际行情轮次未确认健康、来源/时钟/版本不一致")
        progress = await _pending_round_progress(db, order, round_id=fill_round_id,
            source=source, received=received, committed=committed,
            evaluation=evaluation, fill_quantity=fill_quantity)
        evidence["round_progress"] = progress
        if progress["status"] != "validated":
            return rejected(progress["reason"])
        # No await after this sample until the returned owned proof is frozen.
        dispatch = _local_clock(paper._public_order_clock())
        if (dispatch < evaluation or dispatch.date() != evaluation.date() or dispatch >= end
                or not (time(9, 30) <= dispatch.time() < time(11, 30)
                        or time(13) <= dispatch.time() < time(14, 57))):
            return rejected("待单查询后真实时钟回拨、跨日或离开连续竞价")
        ok, reason = paper._execution_quote_status(spot, dispatch.date(), now=dispatch)
        if not ok:
            return rejected(reason)
        reason = _pending_buy_time_reason(order, metadata, now=dispatch)
        if reason:
            return rejected(reason)
        required = _requires_pending_buy_validity(order)
        validity = metadata.get("buy_validity") if required else None
        if queued:
            queue_cancel_at = _local_clock(queue_cancel_at)
            if queue_cancel_at.date() != dispatch.date() or dispatch > queue_cancel_at:
                return rejected("待单查询后已超过原排队撤单截止")
        max_age = max(1, int(getattr(paper.settings, "PAPER_EXECUTION_QUOTE_MAX_AGE_SEC", 90)))
        evidence.update({"status": "validated", "decision_at": decision.isoformat(),
            "evaluated_at": evaluation.isoformat(), "dispatch_validated_at": dispatch.isoformat(),
            "source_quote_at": source.isoformat(), "received_at": received.isoformat(),
            "quote_committed_at": committed.isoformat(), "quote_as_of_at": row.as_of_at.isoformat(),
            "quote_config_version": row.config_version, "quote_code_version": row.code_version,
            "quote_max_age_sec": max_age,
            "quote_expires_at": (source + timedelta(seconds=max_age)).isoformat(),
            "session_end_at": end.isoformat(), "buy_validity_required": required,
            "buy_confirmed_at": validity.get("confirmed_at") if required else None,
            "buy_expires_at": validity.get("expires_at") if required else None,
            "buy_max_execution_delay_sec": validity.get("max_execution_delay_sec") if required else None,
            "queue_cancel_at": queue_cancel_at.isoformat() if queued else None})
        return evidence
    except (ValueError, TypeError, OverflowError):
        return rejected("待单撮合时钟缺失或非法，不能用行情提交时间替代真实时钟")

