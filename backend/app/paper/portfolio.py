"""Forward shared-50000 portfolio: confirmed outbox -> allocation -> real paper service.

The twelve source accounts remain independent controls. This module owns no
indicator, broker, fill, or profit formula. FIFO is explicit, not an optimal-rank
claim; incomparable route scores are retained without cross-route sorting.
"""
import asyncio
import hashlib
import json
from datetime import datetime
from types import SimpleNamespace

from sqlalchemy import select, text

from app.config.settings import settings
from app.models.paper import PaperPortfolioDecision, PaperPortfolioSignal
from app.models.trading import TradeOrder
from app.paper.account_policy import ACCOUNT_NAMES, ROUTE_ACCOUNT_NAMES
from app.paper.portfolio_contract import (
    PORTFOLIO_ACCOUNT, portfolio_active, portfolio_version, entry_version,
    policy_from_settings, validate_signal_times,
)
from app.paper.portfolio_provenance import (
    PortfolioIdentityError, validated_entry_origin,
)
from app.paper.portfolio_wallet import get_portfolio_account, budget_for_origin, order_key

_TERMINAL = ("submitted", "filled", "rejected", "expired", "superseded", "budget_skipped")


def source_health_for_round(source_receipts, round_id):
    """Only explicit, same-round completion authorizes an individual source."""
    receipts = source_receipts if isinstance(source_receipts, dict) else {}
    connected = set(ROUTE_ACCOUNT_NAMES.values())
    health, failures = {}, []
    for name in ACCOUNT_NAMES:
        row = receipts.get(name)
        reason = ""
        if not isinstance(row, dict):
            reason = "source_receipt_missing"
        elif not round_id or row.get("quote_round_id") != round_id:
            reason = "source_receipt_wrong_round"
        elif row.get("status") != "completed":
            reason = "source_not_completed"
        elif name not in connected and row.get("source_scan_status") != "completed":
            reason = "source_scan_not_completed"
        health[name] = not reason
        if reason:
            failures.append({"origin_account": name, "reason_code": reason})
    return health, failures


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, default=str)


def _decision_key(signal_key, round_id):
    return "pd-" + hashlib.sha256((signal_key+"|"+round_id).encode()).hexdigest()


async def _owned_write_phase(db):
    from app.trading.paper_authorization import paper_transaction_active
    if paper_transaction_active(db) or db.new or db.dirty or db.deleted:
        raise RuntimeError("shared allocator needs a clean owned write boundary")
    await db.commit()
    if db.get_bind().dialect.name == "sqlite":
        await db.execute(text("BEGIN IMMEDIATE"))


async def _append_decision(db, signal, *, account_id, context, now, decision,
                           reason_code, reason="", budget=None, order_id=None):
    key = _decision_key(signal.signal_key, str(context["round_id"]))
    existing = await db.scalar(select(PaperPortfolioDecision.id).where(
        PaperPortfolioDecision.decision_key == key))
    if existing is not None:
        return False
    db.add(PaperPortfolioDecision(
        decision_key=key, signal_id=signal.id, portfolio_version=portfolio_version(),
        account_id=account_id, decision_round_id=str(context["round_id"]),
        as_of_at=context["as_of_at"], observed_at=now, decision=decision,
        reason_code=reason_code, reason=reason, order_id=order_id,
        budget_json=_json(budget or {}),
    ))
    await db.commit()
    return True


def _command(signal, *, context, now):
    from app.trading.service import SubmitOrderCommand
    candidate = json.loads(signal.candidate_json)
    policy = json.loads(signal.entry_policy_json)
    details = policy.get("entry_details") or {}
    metadata = {
        "portfolio_signal_key": signal.signal_key,
        "candidate": candidate, "confirmed_at": signal.confirmed_at.isoformat(),
        "stop_loss_price": policy["stop_loss_price"], "block_warn": True,
    }
    queued = details.get("queue_if_limit_up") is True
    if queued:
        from app.api.v1 import paper
        metadata["cancel_time"] = paper._highboard_policy(signal.origin_account)["queue_cancel_time"]
    is_connected = signal.origin_account in ROUTE_ACCOUNT_NAMES.values()
    return SubmitOrderCommand(
        code=signal.code, side="buy", price=float(policy["price"]), quantity=100,
        broker="paper", account_id=PORTFOLIO_ACCOUNT,
        strategy_id="paper-challenger-forward" if is_connected else "paper-auto-short",
        strategy_version=entry_version(signal.origin_version, policy_version=signal.portfolio_version),
        signal_id=signal.source_signal_id, source=signal.source,
        reason=f"单5万元组合分配；来源={signal.origin_account}；信号={signal.signal_key}；仅模拟",
        execute=True, queue_if_limit_up=queued, queue_metadata=metadata if queued else None,
        decision_round_id=str(context["round_id"]), decision_at=now, as_of_at=context["as_of_at"],
        idempotency_key=order_key(signal.signal_key), defer_until_next_round=True,
        # A queue-capable origin with an available offer still waits for the next
        # healthy round; never fall through into immediate execution.
        deferred_metadata=dict(metadata),
        config_version=str(context.get("config_version") or ""),
        code_version=str(context.get("code_version") or ""),
        stop_loss_price=float(policy["stop_loss_price"]),
    )


async def run_shared_portfolio(db, *, allow_entries=True, source_receipts=None,
                               manage_positions=True):
    """Own one new-wallet session. Exceptions/cancellation stay incomplete.

    Invoke after committed source scans. A missing/incomplete source receipt
    prevents that origin's new allocations, not other healthy origins' entries.
    """
    from app.api.v1 import paper
    from app.trading import service

    now = paper._public_order_clock()
    if not portfolio_active(now):
        return {"status": "disabled", "account_name": PORTFOLIO_ACCOUNT, "allocated": 0}
    account = await get_portfolio_account(db)
    account_id = account.id
    if manage_positions:
        await paper.run_paper_position_risk_monitor(db, account_name=PORTFOLIO_ACCOUNT,
                                                   trigger="shared-portfolio-risk")
    async with paper._account_auto_lock(PORTFOLIO_ACCOUNT):
        context = paper._quote_round_context()
        if (not context.get("round_id") or context.get("quality_status") != "ok"
                or not isinstance(context.get("as_of_at"), datetime)):
            return {"status": "degraded", "reason": "healthy_quote_round_missing",
                    "account_name": PORTFOLIO_ACCOUNT, "allocated": 0}
        health, source_failures = source_health_for_round(source_receipts, context["round_id"])
        sources_complete = all(health.values())
        source_reasons = {row["origin_account"]: row["reason_code"] for row in source_failures}
        market_open, market_reason = await paper._paper_order_window_status(now)
        enabled = bool(settings.PAPER_AUTO_TRADE_ENABLED and settings.PAPER_INTRADAY_AUTO_TRADE_ENABLED)
        allow = allow_entries and market_open and enabled
        reason = ("entry_execution_disabled" if not (enabled and allow_entries) else
                  market_reason if not market_open else "")
        terminal = select(PaperPortfolioDecision.id).where(
            PaperPortfolioDecision.signal_id == PaperPortfolioSignal.id,
            PaperPortfolioDecision.decision.in_(_TERMINAL),
        ).exists()
        base = select(PaperPortfolioSignal).where(
            PaperPortfolioSignal.observed_at <= now, ~terminal,
        ).order_by(PaperPortfolioSignal.observed_at, PaperPortfolioSignal.id)
        # Failed-source backlog cannot consume the healthy-source work allowance.
        healthy_names = [name for name, good in health.items() if good]
        healthy_filter = PaperPortfolioSignal.origin_account.in_(healthy_names)
        signals = []
        for partition in (healthy_filter, ~healthy_filter):
            signals.extend((await db.scalars(base.where(partition).limit(128))).all())
        signals.sort(key=lambda signal: (signal.observed_at, signal.id))
        # Superseded samples are explicitly recorded, not removed from history.
        latest = {}
        for signal in signals:
            latest[(signal.origin_account, signal.origin_version, signal.code, signal.source)] = signal.signal_key
        counts = {"considered": 0, "allocated": 0, "rejected": 0, "waiting": 0}
        for signal in signals:
            await asyncio.sleep(0)
            now = paper._public_order_clock()
            # Release read-only snapshots before acquiring SQLite's writer;
            # account creation above stays outside this phase and its init lock.
            await _owned_write_phase(db)
            common = dict(account_id=account_id, context=context, now=now)
            prior = await db.scalar(select(PaperPortfolioDecision.id).where(
                PaperPortfolioDecision.decision_key == _decision_key(signal.signal_key, str(context["round_id"]))))
            if prior is not None:
                await db.commit()
                continue
            counts["considered"] += 1
            existing_order = await db.scalar(select(TradeOrder).where(
                TradeOrder.idempotency_key == order_key(signal.signal_key),
                TradeOrder.account_id == PORTFOLIO_ACCOUNT,
            ))
            if existing_order is not None:
                # A submit committed before its audit receipt; do not submit twice
                # or discard it because the original signal subsequently expired.
                await _append_decision(db, signal, **common,
                    decision="submitted" if existing_order.status in ("pending", "submitted", "partial") else
                             "filled" if existing_order.status == "filled" else "rejected",
                    reason_code="recovered_committed_order", order_id=existing_order.order_id,
                    reason=str(existing_order.status))
                continue
            clock_reason = validate_signal_times(policy=policy_from_settings(settings),
                confirmed_at=signal.confirmed_at, observed_at=signal.observed_at,
                as_of_at=signal.as_of_at, now=now)
            if clock_reason:
                counts["rejected"] += 1
                await _append_decision(db, signal, **common, decision="expired",
                                       reason_code=clock_reason)
                continue
            if not health.get(signal.origin_account, False):
                counts["waiting"] += 1
                await _append_decision(db, signal, **common, decision="source_wait",
                    reason_code=source_reasons.get(signal.origin_account, "source_unknown"))
                continue
            if latest[(signal.origin_account, signal.origin_version, signal.code, signal.source)] != signal.signal_key:
                counts["rejected"] += 1
                await _append_decision(db, signal, **common, decision="superseded",
                    reason_code="newer_same_origin_confirmation",
                    budget={"replaced_by": latest[(signal.origin_account, signal.origin_version, signal.code, signal.source)]})
                continue
            if not allow:
                counts["waiting"] += 1
                await _append_decision(db, signal, **common, decision="source_wait",
                                       reason_code="source_or_execution_unavailable", reason=reason)
                continue
            try:
                cmd = _command(signal, context=context, now=now)
                metadata = cmd.queue_metadata or cmd.deferred_metadata
                origin = await validated_entry_origin(db, cmd, metadata, at=now)
                spot = await paper._spot_by_code(db, signal.code)
                good, quote_reason = paper._execution_quote_status(spot, now.date(), now=now)
                if not good:
                    counts["waiting"] += 1
                    await _append_decision(db, signal, **common, decision="confirmation_wait",
                                           reason_code="quote_not_executable", reason=quote_reason)
                    continue
                if float(spot.price) <= cmd.stop_loss_price:
                    raise PortfolioIdentityError("current quote has reached the original protective stop")
                await service._freeze_pending_buy_validity(db, cmd, metadata, decision_at=now)
                # Reuse original pending validators BEFORE allocating funds, and
                # keep the exact same frozen contract for the actual next round.
                order_view = SimpleNamespace(**vars(cmd), trade_date=now.date())
                invalid = service._pending_buy_time_reason(order_view, metadata, now=now)
                if invalid:
                    raise PortfolioIdentityError(invalid)
                status, why = await service._pending_buy_current_status(
                    db, cmd, metadata, spot, now=now)
                if status != "valid":
                    counts["waiting" if status == "waiting" else "rejected"] += 1
                    await _append_decision(db, signal, **common,
                        decision="confirmation_wait" if status == "waiting" else "rejected",
                        reason_code="original_confirmation_"+status, reason=why)
                    continue
                budget = await budget_for_origin(db, origin=origin, price=cmd.price, now=now)
                if not budget["allowed"]:
                    counts["rejected"] += 1
                    await _append_decision(db, signal, **common, decision="budget_skipped",
                        reason_code=budget["reason_code"], budget=budget)
                    continue
                cmd.quantity = budget["amount"]
                cmd.stop_loss_price = budget["effective_stop_loss_price"]
                for frozen in (cmd.deferred_metadata, cmd.queue_metadata):
                    if frozen is not None:
                        frozen["stop_loss_price"] = cmd.stop_loss_price
                sector = await paper._resolve_candidate_entry_sector(db, origin["candidate"], trade_date=now.date())
                cmd.entry_sector_code = sector.get("entry_sector_code")
                cmd.entry_sector_name = sector.get("entry_sector_name")
                for frozen in (cmd.deferred_metadata, cmd.queue_metadata):
                    if frozen is not None:
                        frozen.update(sector)
            except (PortfolioIdentityError, ValueError, TypeError, KeyError) as exc:
                counts["rejected"] += 1
                await _append_decision(db, signal, **common, decision="rejected",
                                       reason_code="portfolio_entry_evidence_invalid", reason=str(exc))
                continue
            # Do NOT catch submit/commit/cancellation errors as ordinary skips.
            # They must propagate; next pass reconciles this exact idempotency key.
            result = await service.submit_order(db, cmd)
            order = result.get("order") or {}
            accepted = order.get("status") in ("pending", "submitted", "partial", "filled")
            counts["allocated" if accepted else "rejected"] += 1
            # submit_order commits its own ledger/order unit before this audit.
            await _owned_write_phase(db)
            await _append_decision(db, signal, **{**common, "now": paper._public_order_clock()},
                decision="filled" if order.get("status") == "filled" else "submitted" if accepted else "rejected",
                reason_code="order_"+str(order.get("status") or "unknown"), budget=budget,
                order_id=order.get("order_id"), reason=str(order.get("error_message") or ""))
        await db.commit()
        return {"status": "completed" if sources_complete else "degraded",
                "account_name": PORTFOLIO_ACCOUNT, "quote_round_id": context["round_id"],
                "source_scans_complete": sources_complete,
                "source_failures": source_failures, **counts}
