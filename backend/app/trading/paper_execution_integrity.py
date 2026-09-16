"""Read-only, current-day ledger/receipt reconciliation before another paper fill.

The existing per-order legacy replay guard and quote-capacity guard retain their
own scopes. This check starts from BOTH tables so deleting an entire receipt
cannot make durable book consumption disappear. It never repairs history.
"""
import hashlib
import math
from datetime import datetime, time, timedelta

from fastapi import HTTPException
from sqlalchemy import String, cast, select

from app.models.paper import PaperAccount, PaperTradeLog
from app.models.trading import TradeFill, TradeOrder
from app.trading.paper_authorization import paper_transaction_active, _local_clock


async def account_execution_integrity_evidence(db, order, execution):
    if not paper_transaction_active(db):
        raise HTTPException(403, "模拟账本回报对账必须属于当前成交事务")
    from app.trading.service import _json_dumps, _json_loads_dict

    proof = {
        "contract_version": "paper_account_execution_integrity_v1_20260914",
        "scope": "account_trade_day_code_side", "status": "blocked",
        "account_name": str(order.account_id or "default"), "code": order.code, "side": order.side,
        "trade_date": order.trade_date.isoformat(), "order_id": order.order_id,
        "quote_round_id": execution.get("quote_round_id"),
        "checked_trade_count": 0, "checked_receipt_count": 0,
        "automatic_repair_allowed": False, "historical_account_recovery": False,
    }

    def blocked(code, row=None):
        proof.update(reason_code=code,
            reason="同账户成交账本/回报对账未通过；不能据缺失回报释放容量或认领旧成交，保留证据待核对")
        if row is not None:
            proof.update(paper_trade_id=row["t_id"], receipt_id=row["f_id"],
                         prior_order_id=row["f_order_id"])
        return proof

    # Explicit leaf columns, not cached ORM entities or whole-object dumps.
    trade_fields = ("id", "account_id", "code", "trade_type", "price", "amount", "trade_time",
                    "commission", "tax", "signal_id", "realized_pnl", "decision_round_id", "fill_round_id")
    fill_fields = ("id", "fill_id", "order_id", "broker", "code", "side", "price", "quantity",
                   "commission", "tax", "realized_pnl", "broker_trade_id", "raw_json",
                   "decision_round_id", "fill_round_id", "trade_date", "filled_at")
    order_fields = ("order_id", "account_id", "code", "side", "broker", "trade_date", "signal_id")
    columns = ([getattr(PaperTradeLog, f).label("t_"+f) for f in trade_fields] +
               [getattr(TradeFill, f).label("f_"+f) for f in fill_fields] +
               [getattr(TradeOrder, f).label("o_"+f) for f in order_fields] +
               [PaperAccount.account_name.label("t_account_name")])
    day_start = datetime.combine(order.trade_date, time())
    day_end = day_start + timedelta(days=1)
    link = TradeFill.broker_trade_id == cast(PaperTradeLog.id, String)
    ledger_query = (select(*columns).select_from(PaperTradeLog)
        .outerjoin(PaperAccount, PaperAccount.id == PaperTradeLog.account_id)
        .outerjoin(TradeFill, link)
        .outerjoin(TradeOrder, TradeOrder.order_id == TradeFill.order_id)
        .where(PaperAccount.account_name == proof["account_name"],
               PaperTradeLog.code == order.code, PaperTradeLog.trade_type == order.side,
               PaperTradeLog.trade_time >= day_start, PaperTradeLog.trade_time < day_end))
    # Read same-day/same-security receipt candidates across accounts, then select
    # ownership using ledger, order AND frozen contract; don't trust one mutable link.
    receipt_query = (select(*columns).select_from(TradeFill)
        .outerjoin(PaperTradeLog, link)
        .outerjoin(PaperAccount, PaperAccount.id == PaperTradeLog.account_id)
        .outerjoin(TradeOrder, TradeOrder.order_id == TradeFill.order_id)
        .where(TradeFill.code == order.code, TradeFill.trade_date == order.trade_date))
    with db.no_autoflush:
        ledger_rows = (await db.execute(ledger_query.order_by(PaperTradeLog.id, TradeFill.id))).mappings().all()
        receipt_rows = (await db.execute(receipt_query.order_by(TradeFill.id))).mappings().all()
    rows = {(r["t_id"], r["f_id"]): r for r in (*ledger_rows, *receipt_rows)}
    seen_trades, seen_fills, matched = set(), set(), []
    try:
        for row in rows.values():
            raw = _json_loads_dict(row["f_raw_json"])
            prior = raw.get("immediate_execution_evidence") or raw.get("pending_execution_timing")
            prior = prior if isinstance(prior, dict) else {}
            accounts = {row["t_account_name"], row["o_account_id"], prior.get("account_id")} - {None, ""}
            sides = {row["t_trade_type"], row["f_side"], row["o_side"], prior.get("side")}
            if order.side not in sides:
                continue
            if proof["account_name"] not in accounts:
                if accounts:
                    continue  # Known different independent account, not pooled buying power.
                return blocked("unattributed_receipt", row)
            if row["t_id"] is None:
                return blocked("receipt_without_ledger", row)
            if row["f_id"] is None or row["t_id"] in seen_trades:
                return blocked("ledger_without_unique_receipt", row)
            if row["f_id"] in seen_fills:
                return blocked("ledger_receipt_identity_conflict", row)
            seen_trades.add(row["t_id"]); seen_fills.add(row["f_id"])
            if (row["t_account_name"] != proof["account_name"]
                    or row["o_account_id"] != proof["account_name"]
                    or row["o_broker"] != "paper" or row["f_broker"] != "paper"
                    or any(row[key] != order.code for key in ("t_code", "f_code", "o_code"))
                    or any(row[key] != order.side for key in ("t_trade_type", "f_side", "o_side"))
                    or row["f_broker_trade_id"] != str(row["t_id"])
                    or row["o_order_id"] != row["f_order_id"]
                    or row["o_trade_date"] != order.trade_date or row["f_trade_date"] != order.trade_date):
                return blocked("ledger_receipt_identity_conflict", row)
            if (type(row["t_amount"]) is not int or row["t_amount"] < 100 or row["t_amount"] % 100
                    or row["t_amount"] != row["f_quantity"]
                    or _local_clock(row["t_trade_time"]) != _local_clock(row["f_filled_at"])
                    or row["t_trade_time"].date() != order.trade_date
                    or not row["t_fill_round_id"] or not row["t_decision_round_id"]
                    or row["t_fill_round_id"] != row["f_fill_round_id"]
                    or row["t_decision_round_id"] != row["f_decision_round_id"]):
                return blocked("ledger_receipt_identity_conflict", row)
            for tf, ff in (("price", "price"), ("commission", "commission"), ("tax", "tax"),
                           ("realized_pnl", "realized_pnl")):
                tv, fv = row["t_"+tf], row["f_"+ff]
                if tv != fv or (tv is not None and (type(tv) not in (int, float) or not math.isfinite(tv))):
                    return blocked("ledger_receipt_identity_conflict", row)
            if row["t_price"] <= 0 or row["t_commission"] is None or row["t_tax"] is None:
                return blocked("ledger_receipt_identity_conflict", row)
            # Match the original broker trade payload as well: a forged broker ID
            # must not let another order claim an unrelated same-price/quantity fill.
            if (raw.get("id") != row["t_id"] or raw.get("code") != row["t_code"]
                    or raw.get("trade_type") != row["t_trade_type"]
                    or raw.get("price") != row["t_price"] or raw.get("amount") != row["t_amount"]
                    or raw.get("commission") != row["t_commission"] or raw.get("tax") != row["t_tax"]
                    or raw.get("realized_pnl") != row["t_realized_pnl"]
                    or raw.get("signal_id") != row["t_signal_id"]
                    or _local_clock(raw.get("trade_time")) != row["t_trade_time"]
                    or raw.get("decision_round_id") != row["t_decision_round_id"]
                    or raw.get("fill_round_id") != row["t_fill_round_id"]):
                return blocked("ledger_receipt_identity_conflict", row)
            request_id = str(row["f_fill_id"] or "").removeprefix("fill-")
            immediate = prior.get("contract_version") == "immediate_paper_fill_v2_20260914"
            timing = raw.get("ledger_execution_timing")
            if (prior.get("mandatory") is not True
                    or prior.get("status") != ("fillable" if immediate else "validated")
                    or prior.get("fill_price") != row["t_price"]
                    or prior.get("filled_quantity") != row["t_amount"]
                    or not isinstance(timing, dict) or timing.get("status") != "validated"
                    or timing.get("guard_version") != "paper_ledger_timing_v1_20260914"
                    or timing.get("input_contract_version") != prior.get("contract_version")
                    or timing.get("quote_round_id") != row["t_fill_round_id"]
                    or _local_clock(timing.get("before_mutation_checked_at")) != row["t_trade_time"]
                    or timing.get("input_sha256") != hashlib.sha256(_json_dumps(prior).encode()).hexdigest()):
                return blocked("ledger_receipt_identity_conflict", row)
            expected_signal = ((row["o_signal_id"] or row["o_order_id"]) if immediate else None)
            if immediate:
                signal_ok = request_id == row["o_order_id"] and row["t_signal_id"] == expected_signal
            else:
                signal_ok = (prior.get("contract_version") == "pending_paper_fill_timing_v1_20260914"
                    and request_id == prior.get("request_id") and prior.get("order_id") == row["o_order_id"]
                    and row["t_signal_id"] in {request_id, "chlg-"+request_id, "auto-sell-"+request_id,
                                               "auto-tenbagger_midline-"+request_id})
            if (not str(row["f_fill_id"] or "").startswith("fill-") or not signal_ok
                    or prior.get("account_id") != proof["account_name"]
                    or prior.get("code") != row["t_code"] or prior.get("side") != row["t_trade_type"]
                    or prior.get("quote_round_id") != row["t_fill_round_id"]):
                return blocked("ledger_receipt_identity_conflict", row)
            matched.append([row["t_id"], row["f_id"], row["f_fill_id"], row["f_order_id"],
                row["t_account_id"], row["t_fill_round_id"], row["t_amount"], row["t_price"],
                row["t_commission"], row["t_tax"], row["t_realized_pnl"], row["t_trade_time"].isoformat()])
        proof.update(status="validated", reason_code=None,
            reason="当日同账户/证券/方向的既有账本与回报一一对应；不改写历史",
            checked_trade_count=len(seen_trades), checked_receipt_count=len(seen_fills),
            matched_pairs_sha256=hashlib.sha256(_json_dumps(sorted(matched)).encode()).hexdigest())
        return proof
    except (ValueError, TypeError, AttributeError, OverflowError):
        return blocked("ledger_receipt_identity_conflict")
