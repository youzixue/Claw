"""Read-only economic presentation. Never consumed by execution or risk.

Legacy ledger amounts remain authoritative historical records, not corrected in place.
Inventory replay is account-local weighted-cost accounting, not a strategy backtest.
"""
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from sqlalchemy import select
from app.models.paper import PaperPosition, PaperTradeLog, PaperSaleAccounting

ZERO = Decimal("0")
CENT = Decimal(".01")


def _get(row, key, default=None):
    return row.get(key, default) if isinstance(row, dict) else getattr(row, key, default)


def _number(value):
    if value is None or isinstance(value, bool):
        raise ValueError("missing/invalid monetary value")
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("invalid monetary value") from exc
    if not result.is_finite():
        raise ValueError("nonfinite monetary value")
    return result


def _money(value):
    rounded = value.quantize(CENT, rounding=ROUND_HALF_UP)
    return 0.0 if rounded == ZERO else float(rounded)


def _day(value):
    return date.fromisoformat(str(value)[:10])


def accounting_snapshot(account, trades, positions, *, as_of):
    """Pure account-scoped replay; unknown basis fails closed, never becomes zero.

    Gross position PnL uses the stored four-decimal entry VWAP. Economic realized
    uses exact weighted trade cost. Any VWAP rounding difference is separately
    reconciled, not assigned to historical fees. Paid entry fees are allocated
    proportionally to remaining inventory after each sell (including scale-ins).
    """
    aid = _get(account, "id")
    result = {
        "schema": "paper-accounting-v1", "account_id": aid,
        "account_scope": ("closed_legacy" if _get(account, "status") == "closed" else
                          "isolated_challenger" if str(_get(account, "account_name", "")).startswith("challenger") else "primary"),
        "valuation_basis": "persisted_position_current_price_not_a_fresh_quote_guarantee",
        "status": "incomplete", "issues": [], "positions": {}, "trades": {},
        "gross_unrealized_pnl": None, "remaining_entry_fees": None,
        "net_unrealized_pnl": None, "realized_net_pnl": None,
        "realized_ledger_pnl": None, "today_realized_ledger_pnl": None,
        "today_realized_net_pnl": None, "total_economic_pnl": None,
        "legacy_entry_fee_adjustment": None, "unexplained_realized_adjustment": None,
        "cash_reconciliation_residual": None, "asset_reconciliation_residual": None,
        "stored_cost_rounding_bridge": None, "fees_paid": None,
        "eligible_closed_cycles": [], "excluded_cycle_count": 0,
        "closed_cycle_performance": None,
        "note": "只读经济口径；净浮盈仅扣余仓已付买费，不估计未来卖费；今日卖出实现包含跨日持有收益，非今日净值变动；账户全量现金事实与排除实验样本的绩效分列",
    }
    try:
        if any(_get(r, "account_id") != aid for r in [*trades, *positions]):
            raise ValueError("cross_account_input")
        if any(_day(_get(t, "trade_time")) > as_of for t in trades):
            raise ValueError("future_trade_in_snapshot")
        books = {}
        cashflow = fees = realized = ledger = today_realized = today_ledger = ZERO
        legacy_adjustment = other_adjustment = ZERO
        for t in sorted(trades, key=lambda r: (str(_get(r, "trade_time")), _get(r, "id"))):
            code = _get(t, "code")
            qty = _number(_get(t, "amount"))
            price = _number(_get(t, "price"))
            commission, tax = _number(_get(t, "commission")), _number(_get(t, "tax"))
            if commission < 0 or tax < 0:
                raise ValueError("negative_fee")
            fee = commission + tax
            if qty <= 0 or qty != qty.to_integral() or price <= 0 or fee < 0:
                raise ValueError("invalid_trade")
            b = books.setdefault(code, dict(qty=ZERO, cost=ZERO, fee=ZERO, realized=ZERO,
                                           excluded=False, versions=set(), buy_ids=[], buy_date=None))
            b["excluded"] |= bool(_get(t, "forced_probe") or _get(t, "excluded_from_performance"))
            b["versions"].add(_get(t, "strategy_version"))
            fees += fee
            kind = _get(t, "trade_type")
            if kind == "buy":
                b["buy_date"] = b["buy_date"] or str(_get(t, "trade_time"))
                b["qty"] += qty
                b["cost"] += price * qty
                b["fee"] += fee
                b["buy_ids"].append(_get(t, "id"))
                cashflow -= price * qty + fee
            elif kind == "sell":
                if qty > b["qty"]:
                    raise ValueError("sell_without_complete_buy_basis")
                allocated_cost = b["cost"] * qty / b["qty"]
                allocated_fee = b["fee"] * qty / b["qty"]
                net = price * qty - fee - allocated_cost - allocated_fee
                recorded = _number(_get(t, "realized_pnl"))
                delta = net - recorded
                # Known legacy omission is accepted only for dated sells whose
                # individual difference agrees with the actual allocated fee.
                legacy = _day(_get(t, "trade_time")) < date(2026, 9, 7) and abs(delta + allocated_fee) <= CENT
                if legacy:
                    legacy_adjustment += delta
                else:
                    other_adjustment += delta
                realized += net
                ledger += recorded
                if _day(_get(t, "trade_time")) == as_of:
                    today_realized += net
                    today_ledger += recorded
                b["realized"] += net
                b["qty"] -= qty
                b["cost"] -= allocated_cost
                b["fee"] -= allocated_fee
                cashflow += price * qty - fee
                result["trades"][_get(t, "id")] = {
                    "realized_net_pnl": _money(net), "realized_ledger_pnl": _money(recorded),
                    "allocated_entry_fees": _money(allocated_fee),
                    "entry_fee_adjustment_verified": legacy,
                    "scope": "sold_quantity_holding_period_not_daily_mtm",
                }
                if b["qty"] == 0:
                    cycle = dict(code=code, buy_trade_ids=b["buy_ids"], sell_trade_id=_get(t, "id"),
                                 buy_time=b["buy_date"], close_time=str(_get(t, "trade_time")),
                                 strategy_versions=sorted(v for v in b["versions"] if v),
                                 realized_net_pnl=_money(b["realized"]))
                    if b["excluded"]:
                        result["excluded_cycle_count"] += 1
                    else:
                        result["eligible_closed_cycles"].append(cycle)
                    del books[code]
            else:
                raise ValueError("unknown_trade_type")
        gross = remaining_fees = net_open = market_value = rounding_bridge = ZERO
        seen = set()
        for p in positions:
            code = _get(p, "code")
            if code in seen:
                raise ValueError("ambiguous_multiple_open_positions")
            seen.add(code)
            b = books.get(code)
            qty = _number(_get(p, "buy_amount"))
            price = _number(_get(p, "current_price"))
            cost_price = _number(_get(p, "buy_price"))
            if not b or qty != b["qty"] or qty <= 0 or price <= 0 or cost_price <= 0:
                raise ValueError("position_basis_or_valuation_mismatch")
            g = (price - cost_price) * qty
            n = g - b["fee"]
            bridge = cost_price * qty - b["cost"]
            gross += g
            remaining_fees += b["fee"]
            net_open += n
            market_value += price * qty
            rounding_bridge += bridge
            result["positions"][_get(p, "id")] = {
                "gross_unrealized_pnl": _money(g), "remaining_entry_fees": _money(b["fee"]),
                "net_unrealized_pnl": _money(n),
                "net_unrealized_pct": float((n / (cost_price * qty + b["fee"]) * 100).quantize(Decimal(".0001"))),
                "cycle_realized_net_pnl": _money(b["realized"]),
                "cycle_total_net_pnl": _money(b["realized"] + n + bridge),
                "stored_cost_rounding_bridge": _money(bridge),
                "excluded_from_performance": b["excluded"],
                "buy_trade_ids": b["buy_ids"],
            }
        if set(books) != seen:
            raise ValueError("trade_inventory_not_equal_open_positions")
        initial = _number(_get(account, "initial_capital"))
        cash = _number(_get(account, "current_capital"))
        assets = _number(_get(account, "total_assets"))
        cash_residual = cash - initial - cashflow
        asset_residual = assets - cash - market_value
        result.update(
            status="ok" if abs(cash_residual) <= CENT and abs(asset_residual) <= CENT else "unreconciled",
            gross_unrealized_pnl=_money(gross), remaining_entry_fees=_money(remaining_fees),
            net_unrealized_pnl=_money(net_open), realized_net_pnl=_money(realized),
            realized_ledger_pnl=_money(ledger), today_realized_ledger_pnl=_money(today_ledger),
            today_realized_net_pnl=_money(today_realized),
            total_economic_pnl=_money(realized + net_open + rounding_bridge),
            legacy_entry_fee_adjustment=_money(legacy_adjustment),
            unexplained_realized_adjustment=_money(other_adjustment),
            cash_reconciliation_residual=_money(cash_residual),
            asset_reconciliation_residual=_money(asset_residual),
            stored_cost_rounding_bridge=_money(rounding_bridge), fees_paid=_money(fees),
        )
        if abs(cash_residual) > CENT:
            result["issues"].append("现金对账不平衡：实际现金不等于初始资金加历史净交易现金流；仅展示差额，不重复扣净值")
        if abs(asset_residual) > CENT:
            result["issues"].append("资产对账不平衡：总资产不等于现金加持仓市值；仅展示差额，不重复扣净值")
        cycles = result["eligible_closed_cycles"]
        result["closed_cycle_performance"] = {
            "scope": "account_lifetime_all_versions_not_current_protocol",
            "sample_count": len(cycles),
            "net_pnl": _money(sum((_number(c["realized_net_pnl"]) for c in cycles), ZERO)),
            "win_rate": round(sum(c["realized_net_pnl"] > 0 for c in cycles) / len(cycles) * 100, 4) if cycles else None,
            "excluded_cycle_count": result["excluded_cycle_count"],
        }
        if abs(other_adjustment) > CENT:
            result["issues"].append("非已核验旧买费的差额：可能含历史成本分摊/价格舍入，未改写账本")
    except (ValueError, TypeError, InvalidOperation, ZeroDivisionError) as exc:
        # Do not expose partial-position/performance totals as complete evidence.
        result.update(positions={}, trades={}, eligible_closed_cycles=[], excluded_cycle_count=0)
        result["issues"].append(str(exc))
    return result


async def load_accounting(db, account, *, as_of):
    """Only SELECT, no flush/add/commit, scoped by immutable account id."""
    with db.no_autoflush:
        trades = (await db.scalars(select(PaperTradeLog).where(
            PaperTradeLog.account_id == account.id,
        ).order_by(PaperTradeLog.trade_time, PaperTradeLog.id))).all()
        positions = (await db.scalars(select(PaperPosition).where(
            PaperPosition.account_id == account.id, PaperPosition.is_closed.is_(False),
        ))).all()
        evidence = (await db.scalars(select(PaperSaleAccounting).where(
            PaperSaleAccounting.account_id == account.id,
        ))).all()
    result = accounting_snapshot(account, trades, positions, as_of=as_of)
    from app.paper.entry_fee_allocation import sale_evidence_payload
    for proof in evidence:
        trade_result = result["trades"].get(proof.trade_id)
        if trade_result is not None:
            trade_result["entry_fee_allocation"] = sale_evidence_payload(proof)
    # Keep the old exact economic replay separate from each recorded cent allocation.
    result["sale_fee_evidence_scope"] = "new_sales_only_legacy_not_backfilled"
    return result
