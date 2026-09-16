"""Prospective, account-local entry-fee allocation from observed trade inventory.

Cash already paid each commission on its own trade. This only allocates entry
fees to future realized PnL; it never deducts them from cash a second time.
Legacy allocations are replayed economically, NOT certified or overwritten.
"""
import hashlib
import json
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from sqlalchemy import select

VERSION = "remaining_entry_fees_v1"
CENT = Decimal(".01")


def _get(row, key):
    return row.get(key) if isinstance(row, dict) else getattr(row, key, None)


def _decimal(value):
    if value is None or isinstance(value, bool):
        raise ValueError("missing_or_invalid_number")
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        raise ValueError("invalid_number") from None
    if not number.is_finite():
        raise ValueError("nonfinite_number")
    return number


def _clock(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if not isinstance(value, datetime) or value.tzinfo is not None:
        raise ValueError("invalid_trade_clock")
    return value


def _cents(value):
    if value < 0 or value != value.quantize(CENT):
        raise ValueError("invalid_entry_fee_cents")
    return value


def proportional_entry_fee(remaining_fee, sell_quantity, available_quantity):
    """Round each allocation to cents; closing fill consumes the exact remainder."""
    if not 0 < sell_quantity <= available_quantity:
        raise ValueError("sale_quantity_exceeds_inventory")
    if sell_quantity == available_quantity:
        return remaining_fee
    return (remaining_fee * sell_quantity / available_quantity).quantize(CENT, rounding=ROUND_HALF_UP)


def _history_hash(rows):
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def plan_entry_fee(trades, *, account_id, code, position_quantity, sell_quantity, at, prior_evidence=None):
    """Pure, owned output. Missing basis remains unknown, never an invented zero."""
    result = {"version": VERSION, "status": "unknown", "reason": "",
              "account_id": account_id, "code": code, "evaluated_at": at.isoformat() if isinstance(at, datetime) else None,
              "entry_fee_before": None, "allocated_entry_fee": None, "entry_fee_after": None,
              "historical_records_modified": False, "cash_deduction": False,
              "basis": "observed_trade_inventory_cent_allocation_not_historical_fee_charge_certificate"}
    try:
        if not isinstance(at, datetime):
            raise ValueError("invalid_evaluation_clock")
        at = _clock(at)
        if prior_evidence is not None and not isinstance(prior_evidence, dict):
            raise ValueError("invalid_prior_fee_evidence")
        if type(account_id) is not int or account_id <= 0 or not isinstance(code, str) or not code:
            raise ValueError("invalid_account_or_code")
        position_quantity = _decimal(position_quantity)
        sell_quantity = _decimal(sell_quantity)
        if (position_quantity <= 0 or position_quantity != position_quantity.to_integral()
                or sell_quantity <= 0 or sell_quantity != sell_quantity.to_integral()):
            raise ValueError("invalid_inventory_quantity")
        rows = []
        ids = set()
        for row in trades:
            tid = _get(row, "id")
            if type(tid) is not int or tid <= 0 or tid in ids:
                raise ValueError("invalid_or_duplicate_trade_id")
            ids.add(tid)
            if _get(row, "account_id") != account_id or _get(row, "code") != code:
                raise ValueError("cross_account_or_code_input")
            clock = _clock(_get(row, "trade_time"))
            if clock > at:
                raise ValueError("future_trade_in_fee_basis")
            rows.append((clock, tid, row))
        if any(key not in {tid for _, tid, row in rows if _get(row, "trade_type") == "sell"}
               for key in (prior_evidence or {})):
            raise ValueError("orphan_fee_evidence")
        rows.sort(key=lambda item: (item[0], item[1]))
        qty = fee = Decimal(0)
        total_buys = allocated = Decimal(0)
        legacy_sells = 0
        current_ids = []
        owned_history = []
        for clock, tid, row in rows:
            amount, price = _decimal(_get(row, "amount")), _decimal(_get(row, "price"))
            if amount <= 0 or amount != amount.to_integral() or price <= 0:
                raise ValueError("invalid_trade_quantity_or_price")
            commission = _cents(_decimal(_get(row, "commission")))
            tax = _cents(_decimal(_get(row, "tax")))
            kind = _get(row, "trade_type")
            owned_history.append([tid, account_id, code, kind,
                *[format(v.normalize(), "f") for v in (amount, price, commission, tax)], clock.isoformat()])
            current_ids.append(tid)
            if kind == "buy":
                qty += amount
                fee += commission + tax
                total_buys += commission + tax
            elif kind == "sell":
                portion = proportional_entry_fee(fee, amount, qty)
                proof = (prior_evidence or {}).get(tid)
                if proof is not None:
                    if (not isinstance(proof, dict) or proof.get("version") != VERSION
                            or proof.get("status") != "known"
                            or proof.get("sale_trade_id") != tid
                            or proof.get("input_sha256") != _history_hash(owned_history[:-1])
                            or proof.get("account_id") != account_id or proof.get("code") != code
                            or proof.get("position_quantity") != int(qty)
                            or proof.get("sell_quantity") != int(amount)
                            or _decimal(proof.get("entry_fee_before")) != fee
                            or _decimal(proof.get("allocated_entry_fee")) != portion
                            or _decimal(proof.get("entry_fee_after")) != fee - portion):
                        raise ValueError("prior_fee_evidence_conflict")
                else:
                    legacy_sells += 1
                fee -= portion
                allocated += portion
                qty -= amount
                if qty == 0:
                    if fee != 0 or allocated != total_buys:
                        raise ValueError("closed_cycle_fee_not_conserved")
                    # Completed cycles cannot leak entry fees into the next entry.
                    fee = total_buys = allocated = Decimal(0)
                    legacy_sells = 0
                    current_ids = []
            else:
                raise ValueError("unknown_trade_type")
        if qty != position_quantity:
            raise ValueError("trade_inventory_does_not_match_position")
        portion = proportional_entry_fee(fee, sell_quantity, qty)
        result.update(status="known", reason="",
            position_quantity=int(qty), sell_quantity=int(sell_quantity),
            entry_fee_before=float(fee), allocated_entry_fee=float(portion),
            entry_fee_after=float(fee-portion),
            cycle_entry_fees_paid=float(total_buys), cycle_replayed_allocated_fees=float(allocated),
            legacy_sell_count_in_open_cycle=legacy_sells,
            prior_sale_allocations_evidenced=legacy_sells == 0,
            trade_ids_in_open_cycle=current_ids,
            input_sha256=_history_hash(owned_history))
    except (ValueError, TypeError, InvalidOperation, ZeroDivisionError, OverflowError) as exc:
        result["reason"] = str(exc)
    return result


async def load_entry_fee_plan(db, position, *, sell_quantity, at):
    """Read the whole account/code chain; no hiding future/invalid rows with WHERE."""
    from app.models.paper import PaperTradeLog, PaperSaleAccounting
    with db.no_autoflush:
        rows = list((await db.scalars(select(PaperTradeLog).where(
            PaperTradeLog.account_id == position.account_id,
            PaperTradeLog.code == position.code,
        ).order_by(PaperTradeLog.trade_time, PaperTradeLog.id))).all())
        proofs = list((await db.scalars(select(PaperSaleAccounting).where(
            PaperSaleAccounting.account_id == position.account_id,
            PaperSaleAccounting.code == position.code,
        ))).all())
    parsed = {}
    for proof in proofs:
        try:
            payload = json.loads(proof.payload_json)
        except (TypeError, ValueError):
            payload = None
        # Present-but-corrupt is not an absent legacy proof.
        parsed[proof.trade_id] = payload if isinstance(payload, dict) and proof.version == VERSION else {}
    return plan_entry_fee(rows, account_id=position.account_id, code=position.code,
        position_quantity=position.buy_amount, sell_quantity=sell_quantity, at=at,
        prior_evidence=parsed)


def sale_evidence_payload(proof):
    """Owned JSON display; unknown evidence is not an inferred historical rule."""
    try:
        payload = json.loads(proof.payload_json)
        if (proof.version != VERSION or not isinstance(payload, dict)
                or payload.get("version") != VERSION or payload.get("status") != "known"
                or payload.get("sale_trade_id") != proof.trade_id
                or payload.get("account_id") != proof.account_id or payload.get("code") != proof.code):
            raise ValueError("invalid_sale_fee_evidence")
        before, portion, after = [_cents(_decimal(payload.get(key))) for key in (
            "entry_fee_before", "allocated_entry_fee", "entry_fee_after")]
        quantity, sale = payload.get("position_quantity"), payload.get("sell_quantity")
        if (type(quantity) is not int or type(sale) is not int
                or not 0 < sale <= quantity or before != portion + after
                or portion != proportional_entry_fee(before, sale, quantity)):
            raise ValueError("invalid_sale_fee_allocation")
        digest = payload.get("input_sha256")
        if (not isinstance(digest, str) or len(digest) != 64
                or any(c not in "0123456789abcdef" for c in digest)):
            raise ValueError("invalid_fee_input_digest")
        return payload
    except (ValueError, TypeError, AttributeError, InvalidOperation):
        return {"status": "unknown", "reason": "invalid_sale_fee_evidence"}


async def load_sale_fee_evidence(db, trade):
    from app.models.paper import PaperSaleAccounting
    with db.no_autoflush:
        proof = await db.scalar(select(PaperSaleAccounting).where(
            PaperSaleAccounting.trade_id == trade.id,
            PaperSaleAccounting.account_id == trade.account_id,
            PaperSaleAccounting.code == trade.code))
    return (sale_evidence_payload(proof) if proof is not None else
            {"status": "unknown", "reason": "historical_sale_has_no_frozen_fee_allocation"})


def new_sale_accounting(trade, plan):
    """Create only a new sale's evidence, in the same ledger flush/commit."""
    from app.models.paper import PaperSaleAccounting
    payload = dict(plan)
    payload.update(sale_trade_id=trade.id, sale_price=trade.price,
                   sale_commission=trade.commission, sale_tax=trade.tax,
                   realized_ledger_pnl=trade.realized_pnl)
    return PaperSaleAccounting(trade_id=trade.id, account_id=trade.account_id,
        code=trade.code, version=VERSION, recorded_at=datetime.now(),
        payload_json=json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False))
