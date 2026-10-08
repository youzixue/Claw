"""Transactional confirmed-signal outbox for the single-wallet paper portfolio.

Called at the existing technical-confirmation boundary, never from notifications,
historical fills, or limit-up winners. The caller owns commit/rollback.
"""
import json
import logging
import math
from datetime import datetime

from sqlalchemy import select

from app.config.settings import settings
from app.data.fund_flow_clock import local_clock
from app.paper.account_policy import ACCOUNT_NAMES, ROUTE_ACCOUNT_NAMES
from app.paper.portfolio_contract import portfolio_active, portfolio_version, make_signal_key

logger = logging.getLogger(__name__)


def _json(value):
    def encode(item):
        if isinstance(item, datetime):
            return item.isoformat()
        raise TypeError("portfolio ingress accepts owned JSON values only")
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      allow_nan=False, default=encode)


def ingress_clock_reason(*, confirmed_at, observed_at, as_of_at, max_age_sec):
    clocks = [local_clock(value) for value in (confirmed_at, observed_at, as_of_at)]
    if any(value is None for value in clocks):
        return "confirmation/observation/quote clock missing or invalid"
    confirmed, observed, quote = clocks
    if not (quote.date() == confirmed.date() == observed.date()):
        return "cross-session confirmation cannot enter the portfolio"
    if not quote <= confirmed <= observed:
        return "quote/confirmation/observation clocks are not causal"
    if (not isinstance(max_age_sec, (int, float)) or isinstance(max_age_sec, bool)
            or not math.isfinite(max_age_sec) or max_age_sec <= 0):
        return "invalid original confirmation TTL"
    if (observed - confirmed).total_seconds() > max_age_sec:
        return "confirmation already expired before portfolio publication"
    return ""


async def capture_confirmed_signal(
    db, *, account, source, candidate, source_signal_id, confirmed_at,
    quote_context, price, stop_loss_price, entry_details=None,
    shadow_event_key="", observed_at=None,
):
    """Append once in the source transaction, before its independent cash gates.

    A disabled portfolio is a strict no-I/O path. Failure to append is not
    represented as a successful publication. No commit, SAVEPOINT or notification.
    """
    from app.api.v1 import paper
    from app.models.paper import PaperPortfolioSignal
    from app.paper.account_policy import account_parameter_snapshot, challenger_execution_policy

    observed = local_clock(observed_at if observed_at is not None else paper._public_order_clock())
    if observed is None or not portfolio_active(observed):
        return None
    confirmed = local_clock(confirmed_at)
    account_name = str(getattr(account, "account_name", "") or "")
    if (account_name not in ACCOUNT_NAMES or getattr(account, "status", "") != "active"
            or type(getattr(account, "id", None)) is not int):
        raise ValueError("portfolio publication needs an active original strategy account")
    if not portfolio_active(confirmed):
        return None  # Never import pre-activation observations.
    route = next((key for key, name in ROUTE_ACCOUNT_NAMES.items() if name == account_name), None)
    ttl = (challenger_execution_policy(route)["max_execution_delay_sec"]
           if route else settings.PAPER_PENDING_BUY_MAX_AGE_SEC)
    # Challenger confirmation time is the immutable event's time; the quote
    # which revalidates it may be newer. Preserve both clocks, not a renewed TTL.
    quote_asof = local_clock(quote_context.get("as_of_at"))
    decision_at = local_clock(quote_context.get("committed_at"))
    if shadow_event_key:
        reason = ingress_clock_reason(confirmed_at=decision_at, observed_at=observed,
                                      as_of_at=quote_asof, max_age_sec=settings.PAPER_EXECUTION_QUOTE_MAX_AGE_SEC)
        if (confirmed is None or decision_at is None or confirmed.date() != decision_at.date()
                or confirmed > decision_at or (observed - confirmed).total_seconds() > ttl):
            reason = "immutable event is missing, future, cross-day or expired"
    else:
        reason = ingress_clock_reason(confirmed_at=confirmed, observed_at=observed,
                                      as_of_at=quote_asof, max_age_sec=ttl)
    if reason:
        logger.warning("[paper-portfolio] ingress rejected %s/%s: %s",
                       account_name, candidate.get("code") if isinstance(candidate, dict) else "", reason)
        return None
    round_id = str(quote_context.get("round_id") or "")
    if (not round_id or not source_signal_id or not isinstance(candidate, dict)
            or not str(candidate.get("code") or "") or not source
            or (route and (route != source or not shadow_event_key))
            or (not route and shadow_event_key)):
        raise ValueError("portfolio publication lacks the original source identity")
    if any(isinstance(value, bool) or not isinstance(value, (int, float))
           or not math.isfinite(value) or value <= 0 for value in (price, stop_loss_price)):
        raise ValueError("portfolio publication requires a finite price and protective stop")
    if stop_loss_price >= price:
        raise ValueError("portfolio entry is at or below its protective stop")
    version = paper._strategy_version(account_name)
    policy_version = portfolio_version()
    code = str(candidate["code"])
    key = make_signal_key(
        source=source, origin_account=account_name, origin_version=version,
        decision_round_id=round_id, source_signal_id=source_signal_id,
        shadow_event_key=str(shadow_event_key or "") or None,
        origin_account_id=account.id, portfolio_version=policy_version,
    )
    snapshot = account_parameter_snapshot(account_name)
    payload = {**candidate, "code": code, "_source": source}
    entry = {
        "schema": "portfolio_origin_entry_v1", "price": float(price),
        "stop_loss_price": float(stop_loss_price), "max_execution_delay_sec": ttl,
        "parameters": snapshot,
        "exit_parameters": paper._strategy_sell_params_by_name(account_name),
        "exit_mode": ("midline" if paper._base_strategy_account(account_name)
                      in (paper.PAPER_ACCOUNT_TENBAGGER, paper.PAPER_ACCOUNT_REVERSAL)
                      or account_name == paper.PAPER_ACCOUNT_CHALLENGER_A else "short"),
        "entry_details": dict(entry_details or {}),
        "notification": {
            "schema": "portfolio_buy_point_ingress_v1",
            "notification_allowed": bool(settings.PAPER_BUY_POINT_PUSH_ENABLED and settings.PUSH_ENABLED),
            "price_basis": "original_order_reference",
        },
    }
    candidate_json, entry_json = _json(payload), _json(entry)
    existing = await db.scalar(select(PaperPortfolioSignal).where(PaperPortfolioSignal.signal_key == key))
    if existing is not None:
        # First evidence wins; a later callback cannot silently publish changed
        # prices/policy under the old key. Retry observation time never refreshes it.
        # Notification switches are not trading evidence. First envelope wins,
        # including legacy rows with no envelope; never rewrite on a later poll.
        original_entry = json.loads(existing.entry_policy_json)
        original_entry.pop("notification", None)
        comparison_entry = {k: v for k, v in entry.items() if k != "notification"}
        if (existing.candidate_json != candidate_json or _json(original_entry) != _json(comparison_entry)
                or existing.confirmed_at != confirmed or existing.as_of_at != quote_asof):
            raise ValueError("portfolio confirmation key conflicts with frozen original evidence")
        return existing
    row = PaperPortfolioSignal(
        signal_key=key, origin_account=account_name, origin_account_id=account.id,
        origin_version=version, portfolio_version=policy_version, code=code,
        name=str(candidate.get("name") or code), source=str(source),
        source_signal_id=str(source_signal_id), shadow_event_key=str(shadow_event_key or "") or None,
        confirmed_at=confirmed, observed_at=observed, as_of_at=quote_asof,
        decision_round_id=round_id, candidate_json=candidate_json,
        entry_policy_json=entry_json,
        exit_policy_json=_json({"exit_parameters": entry["exit_parameters"], "exit_mode": entry["exit_mode"]}),
        created_at=observed,
    )
    db.add(row)
    await db.flush()
    return row
