"""Owned read-only projections, NOT historical frozen position inputs.

Current schema cannot prove per-frame quantity, corporate actions or calendar
first knowledge. Never synthesize those from today's inventory or calendar.
"""
import asyncio
from collections import defaultdict
from dataclasses import asdict
from datetime import date, datetime, time, timedelta
import json
from types import SimpleNamespace

from sqlalchemy import select

from app.config.settings import settings
from app.models.paper import PaperAccount, PaperPosition, PaperTradeLog
from app.models.trading import TradeOrder, TradeFill
from app.models.governance import TradeCalendarModel
from app.models.stock import QuoteRound
from app.paper.post_exit_research import QuoteArchiveRef, PostExitPolicy, load_quote_archive, _fill_identity
from app.paper.profit_protection_research import (
    build_profit_protection_report as evaluate_frozen_inputs, _hash, _owned, _clock, _num,
)

POSITION = ("id", "account_id", "code", "buy_time", "buy_price", "buy_amount",
            "strategy_version", "stop_loss_price", "is_closed")
TRADE = ("id", "account_id", "code", "trade_type", "price", "amount", "trade_time",
         "commission", "tax", "strategy_version", "decision_round_id", "fill_round_id",
         "forced_probe", "excluded_from_performance")
FILL = ("id", "fill_id", "order_id", "broker", "broker_trade_id", "code", "side",
        "price", "quantity", "commission", "tax", "filled_at", "decision_round_id", "fill_round_id")
ORDER = ("order_id", "broker", "account_id", "code", "side", "price", "quantity",
         "created_at", "strategy_version", "decision_round_id", "risk_json")
BLOCKERS = (
    "historical_position_revision_unavailable",
    "per_frame_sellable_quantity_unavailable",
    "corporate_action_and_price_basis_unproven",
    "calendar_first_knowledge_unavailable",
    "original_exit_gate_per_frame_unavailable",
    "trade_fill_observed_at_unavailable",
)


def _project(row, fields):
    return {key: getattr(row, key) for key in fields}


def _entry(raw):
    try:
        obj = json.loads(raw or "{}")
        value = obj.get("experiment_entry") if type(obj) is dict else None
    except (ValueError, TypeError):
        value = None
    if type(value) is not dict:
        return {}
    # Do not expose unrelated risk payload or credentials.
    return {k: value.get(k) for k in (
        "strategy_version", "observed_at", "exit_parameters", "account_id",
        "account_name", "position_id", "decision_round_id", "fill_round_id")}


def _audit(position, account_name, trades, pairs):
    """Consistency of supplied present-day rows, never a frozen identity certificate."""
    reasons = list(BLOCKERS)
    relevant = [t for t in trades if t["account_id"] == position["account_id"]
                and t["code"] == position["code"] and t["trade_time"] >= position["buy_time"]]
    result = {"current_position": position, "account_name": account_name,
              "trade_ids": [t["id"] for t in relevant], "status": "blocked",
              "entry_order": None, "entry_policy": None, "actual_entry_fees": None,
              "constant_lot_consistency": None, "execution_permission": None,
              "cost_after_executable_return": None, "reasons": reasons}
    if not account_name:
        reasons.append("account_instance_missing")
    if len(relevant) != 1 or relevant[0]["trade_type"] != "buy":
        reasons.append("constant_lot_trade_history_not_single_buy")
        return result
    trade = relevant[0]
    matched = [(f, o) for f, o in pairs if f["broker_trade_id"] == str(trade["id"])]
    if len(matched) != 1:
        reasons.append("fill_missing_or_ambiguous")
        return result
    fill, order = matched[0]
    result["entry_order"] = {k: v for k, v in order.items() if k != "risk_json"}
    result["entry_fill"] = fill
    entry = _entry(order["risk_json"])
    result["entry_policy"] = entry
    result["entry_policy_raw_hash"] = _hash(order["risk_json"])
    error = _fill_identity(SimpleNamespace(**trade),
                           [(SimpleNamespace(**fill), SimpleNamespace(**order))])
    if error:
        reasons.append(error)
    if order["account_id"] != account_name:
        reasons.append("order_account_name_conflict")
    if (trade["trade_time"] != position["buy_time"]
            or trade["strategy_version"] != position["strategy_version"]
            or not position["strategy_version"]
            or trade["amount"] != position["buy_amount"]
            or _num(position["buy_price"]) is None
            or _num(trade["price"]) is None
            or abs(position["buy_price"] - trade["price"]) > 1e-6
            or position["is_closed"] is not False):
        reasons.append("current_position_entry_conflict")
    if trade["forced_probe"] is not False or trade["excluded_from_performance"] is not False:
        reasons.append("forced_or_excluded_entry")
    if (not trade["decision_round_id"]
            or trade["decision_round_id"] != fill["decision_round_id"]
            or trade["decision_round_id"] != order["decision_round_id"]):
        reasons.append("decision_round_unproven")
    observed = _clock(entry.get("observed_at"))
    rules = entry.get("exit_parameters")
    if (observed is None or observed > trade["trade_time"]
            or entry.get("strategy_version") != position["strategy_version"]
            or type(rules) is not dict
            or type(rules.get("max_hold_days")) is not int or rules["max_hold_days"] <= 0
            or any(_num(rules.get(k)) is None or rules[k] <= 0
                   for k in ("stop_loss_pct", "take_profit_pct"))):
        reasons.append("frozen_exit_policy_incomplete")
    # An account name and broker trade ID reconcile the visible ledger, but do
    # not manufacture the entry snapshot's position/account instance reference.
    if (entry.get("account_id") != position["account_id"]
            or entry.get("account_name") != account_name
            or entry.get("position_id") != position["id"]):
        reasons.append("frozen_entry_instance_unproven")
    fees = [_num(trade[k]) for k in ("commission", "tax")]
    if any(v is None or v < 0 for v in fees):
        reasons.append("entry_fee_invalid")
    if not error and order["account_id"] == account_name and all(v is not None and v >= 0 for v in fees):
        result["actual_entry_fees"] = sum(fees)  # log and fill are NOT added together
    if len(reasons) == len(BLOCKERS):
        result["constant_lot_consistency"] = True
    return result


async def build_profit_protection_inputs(db, *, start_date, end_date, as_of,
                                         archive_root=None, policies):
    """Caller owns read-only transaction (SQLite mode=ro/query_only/BEGIN).

    Date window selects current position rows by original buy_time. Deleted
    positions and historical inventory snapshots cannot be reconstructed here.
    No business writes/flush/commit; no current strategy defaults or calendar IO.
    """
    if (type(start_date) is not date or type(end_date) is not date
            or _clock(as_of) is None or start_date > end_date or end_date > as_of.date()):
        raise ValueError("invalid explicit local research boundary")
    policies = tuple(policies)
    empty = evaluate_frozen_inputs([], [], [], as_of=as_of, policies=policies)
    begin = datetime.combine(start_date, time())
    stop = min(datetime.combine(end_date + timedelta(days=1), time()), as_of)
    with db.no_autoflush:
        positions = (await db.scalars(select(PaperPosition).where(
            PaperPosition.buy_time >= begin, PaperPosition.buy_time <= stop,
            PaperPosition.buy_time <= as_of).order_by(PaperPosition.id))).all()
        # Stop is an exclusive next-day boundary, except the inclusive as_of.
        positions = [_project(p, POSITION) for p in positions if p.buy_time.date() <= end_date]
        account_ids = {p["account_id"] for p in positions}
        accounts = dict((await db.execute(select(PaperAccount.id, PaperAccount.account_name)
                          .where(PaperAccount.id.in_(account_ids)))).all())
        trades = [_project(t, TRADE) for t in (await db.scalars(select(PaperTradeLog).where(
            PaperTradeLog.account_id.in_(account_ids), PaperTradeLog.trade_time >= begin,
            PaperTradeLog.trade_time <= as_of).order_by(PaperTradeLog.trade_time, PaperTradeLog.id))).all()]
        ids = [str(t["id"]) for t in trades]
        rows = (await db.execute(select(TradeFill, TradeOrder).join(
            TradeOrder, TradeOrder.order_id == TradeFill.order_id).where(
            TradeFill.broker == "paper", TradeOrder.broker == "paper",
            TradeFill.broker_trade_id.in_(ids), TradeFill.filled_at <= as_of,
            TradeOrder.created_at <= as_of))).all()
        pairs = [(_project(f, FILL), _project(o, ORDER)) for f, o in rows]
        calendar = [{"trade_date": d, "is_trade_day": state}
                    for d, state in (await db.execute(select(
                        TradeCalendarModel.trade_date, TradeCalendarModel.is_trade_day).where(
                        TradeCalendarModel.trade_date >= start_date,
                        TradeCalendarModel.trade_date <= as_of.date()).order_by(
                        TradeCalendarModel.trade_date))).all()]
        rounds = (await db.scalars(select(QuoteRound).where(
            QuoteRound.trade_date >= start_date, QuoteRound.trade_date <= as_of.date(),
            QuoteRound.committed_at <= as_of).order_by(QuoteRound.committed_at, QuoteRound.id))).all()
        refs = tuple(QuoteArchiveRef(**_project(r, (
            "round_id", "trade_date", "committed_at", "source", "quality_status",
            "archive_status", "archive_path", "focus_path", "config_version", "code_version")))
            for r in rounds)
    codes = defaultdict(set)
    for p in positions:
        day = p["buy_time"].date()
        while day <= as_of.date():
            codes[day].add(p["code"])
            day += timedelta(days=1)
    series, manifest = await asyncio.to_thread(
        load_quote_archive, refs, {d: frozenset(c) for d, c in codes.items()},
        root=archive_root if archive_root is not None else settings.QUOTE_ROUND_ARCHIVE_DIR,
        policy=PostExitPolicy())
    result = _owned({
        "schema_version": "research:profit_protection_inputs_v1",
        "status": "blocked" if positions else "empty",
        "as_of": as_of, "start_date": start_date, "end_date": end_date,
        "scope": "current_read_only_projection_not_frozen_historical_inputs",
        "selection": "current_position_rows_bought_in_window_including_closed",
        "policies": [asdict(p) for p in policies],
        "positions": [], "samples": [], "calendar": [],
        "current_calendar_projection": calendar,
        "position_audits": [_audit(p, accounts.get(p["account_id"]), trades, pairs) for p in positions],
        "trade_projection": trades,
        "fill_order_evidence_hash": _hash(pairs),
        "archive_manifest": manifest,
        "archive_quote_counts": [{"trade_date": d, "code": c, "rows": len(rows)}
                                 for (d, c), rows in sorted(series.items())],
        "archive_availability": "retrospective_archive_availability_not_historical_ready_at",
        "archive_role": "material_inventory_only_not_position_path",
        "limitations": list(BLOCKERS) + ["deleted_positions_not_in_denominator",
            "trade_time_not_fill_observation_time", "no_daily_high_or_current_inventory_backfill"],
        "frozen_report": empty,
    })
    result["data_hash"] = _hash(result)
    return result


async def build_profit_protection_report(db, *, start_date, end_date, as_of,
                                         archive_root=None, policies):
    """Same blocked-input envelope, with the original pure report nested."""
    return await build_profit_protection_inputs(db, start_date=start_date, end_date=end_date,
        as_of=as_of, archive_root=archive_root, policies=policies)
