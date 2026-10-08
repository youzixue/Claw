"""Pure shared-wallet experiment contract. No DB, execution, scoring or app startup.

Caller must hold the wallet transaction lock and supply fresh ledger aggregates.
Exposure is market value + pending BUY notional (no netting pending SELLs).
reserved_cash includes all pending BUY notional AND their remaining fees; cash
is the ledger's gross cash, before that reservation. Route caps are fractions.
"""
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_CEILING
import hashlib
import math
import json
from zoneinfo import ZoneInfo

PORTFOLIO_ACCOUNT = "shared_50k"
PORTFOLIO_VERSION = "shared_50k_v5_source_isolated_delivery"
_SH = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class PortfolioPolicy:
    enabled: bool = False
    activation_at: str | datetime | None = None
    initial_capital: float = 50000
    max_exposure_ratio: float = .8
    max_symbol_ratio: float = .2
    max_symbols: int = 5
    max_daily_new_symbols: int = 5
    signal_ttl_seconds: int = 120
    commission_rate: float = .0003
    min_commission: float = 5


@dataclass(frozen=True)
class BudgetDecision:
    allowed: bool
    reason_code: str
    amount: int = 0
    notional: float = 0
    fee: float = 0
    reserve: float = 0


def _number(value):
    if isinstance(value, bool) or value is None:
        raise ValueError("unknown numeric input")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError("invalid numeric input") from exc
    if not result.is_finite() or result < 0:
        raise ValueError("invalid numeric input")
    return result


def _time(value):
    # Require seconds: a calendar date is not an activation instant.
    if isinstance(value, str):
        if len(value) < 19 or value[10] not in ("T", " "):
            raise ValueError("precise timestamp required")
        value = datetime.fromisoformat(value)
    if not isinstance(value, datetime):
        raise ValueError("timestamp required")
    return value.replace(tzinfo=_SH) if value.tzinfo is None else value.astimezone(_SH)


def policy_error(policy):
    """Return a stable fail-closed policy reason; no environment reads."""
    if policy.enabled is not True:
        return "portfolio_disabled"
    try:
        _time(policy.activation_at)
        if _number(policy.initial_capital) != 50000:
            return "invalid_portfolio_policy"
        if not 0 < _number(policy.max_exposure_ratio) <= Decimal(".8"):
            return "invalid_portfolio_policy"
        if not 0 < _number(policy.max_symbol_ratio) <= Decimal(".2"):
            return "invalid_portfolio_policy"
        for value, maximum in ((policy.max_symbols,5),(policy.max_daily_new_symbols,5),
                               (policy.signal_ttl_seconds,120)):
            if type(value) is not int or not 0 < value <= maximum:
                return "invalid_portfolio_policy"
        if _number(policy.min_commission) < 5 or _number(policy.commission_rate) < Decimal(".0003"):
            return "invalid_portfolio_policy"
    except (ValueError, TypeError, OverflowError):
        return "invalid_portfolio_policy"
    return None


def policy_from_settings(settings):
    """Copy explicit config leaves; validate with policy_error before use."""
    return PortfolioPolicy(
        enabled=settings.PAPER_PORTFOLIO_ENABLED,
        activation_at=settings.PAPER_PORTFOLIO_ACTIVATION_AT,
        initial_capital=settings.PAPER_PORTFOLIO_INITIAL_CAPITAL,
        max_exposure_ratio=settings.PAPER_PORTFOLIO_MAX_EXPOSURE_RATIO,
        max_symbol_ratio=settings.PAPER_PORTFOLIO_MAX_SYMBOL_RATIO,
        max_symbols=settings.PAPER_PORTFOLIO_MAX_SYMBOLS,
        max_daily_new_symbols=settings.PAPER_PORTFOLIO_MAX_DAILY_NEW_SYMBOLS,
        signal_ttl_seconds=settings.PAPER_PORTFOLIO_SIGNAL_TTL_SECONDS,
        commission_rate=settings.PAPER_COMMISSION_RATE,
        min_commission=settings.PAPER_MIN_COMMISSION,
    )


def portfolio_policy():
    """Runtime config adapter; import settings lazily, never app startup."""
    from app.config.settings import settings
    return asdict(policy_from_settings(settings))


def portfolio_active(at):
    """Enabled, valid policy with a precise activation no later than at."""
    try:
        policy = PortfolioPolicy(**portfolio_policy())
        return policy_error(policy) is None and _time(policy.activation_at) <= _time(at)
    except (ValueError, TypeError, OverflowError):
        return False


def portfolio_version():
    """Content-address constraints/activation; enabled is only a lifecycle switch.

    80/20/5 are conservative experiment ceilings, not validated return optima.
    Invalid policy cannot activate even if a diagnostic identity is computable.
    """
    policy = portfolio_policy()
    policy.pop("enabled")
    try:
        policy["activation_at"] = _time(policy["activation_at"]).isoformat()
    except (ValueError, TypeError, OverflowError):
        pass  # Preserve the invalid configured value; never authorize it.
    payload = {"protocol": PORTFOLIO_VERSION, "account": PORTFOLIO_ACCOUNT, "policy": policy}
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False,
                           separators=(",", ":"), allow_nan=False)
    return "s50k_v1_" + hashlib.sha256(canonical.encode()).hexdigest()[:20]


def entry_version(origin_version, *, policy_version=None):
    """Reproduce old holding identity using its immutable policy version on exits."""
    if not isinstance(origin_version, str) or not origin_version.strip():
        raise ValueError("origin_version required")
    version = portfolio_version() if policy_version is None else policy_version
    if not isinstance(version, str) or not version.strip() or len(version) > 31:
        raise ValueError("policy_version must be nonempty and at most 31 characters")
    digest = hashlib.sha256((version + "|" + origin_version).encode()).hexdigest()[:32]
    return version + ":" + digest


def validate_signal_times(*, policy, confirmed_at, as_of_at, observed_at, now):
    """New-allocation SLA only: same Shanghai day, fresh and post activation.

    The 120s ceiling is independent of the original strategy TTL. Caller must
    additionally enforce the frozen source TTL (effective min of the two).
    This function must NOT replace the original pending-order TTL contract.
    observed_at is the actual append wall clock, NOT the quote/decision clock.
    Caller supplies a real current wall clock as now; no fabricated backfill clock.
    """
    error = policy_error(policy)
    if error:
        return error
    try:
        current, activation = _time(now), _time(policy.activation_at)
        confirmed, as_of, observed = map(_time,(confirmed_at,as_of_at,observed_at))
    except (ValueError, TypeError, OverflowError):
        return "invalid_signal_time"
    for stamp in (confirmed,as_of,observed):
        if stamp > current:
            return "future_signal_time"
        if stamp.date() != current.date():
            return "cross_day_signal"
        if stamp < activation:
            return "pre_activation_signal"
        if (current-stamp).total_seconds() > policy.signal_ttl_seconds:
            return "expired_signal"
    if observed < max(confirmed,as_of):
        return "invalid_observation_order"
    return None


def make_signal_key(*, source, origin_account, origin_version,
                    decision_round_id, source_signal_id=None, shadow_event_key=None,
                    origin_account_id=None, portfolio_version=None):
    """Round-specific immutable identity; never derive from score or wallet state."""
    required = (source, origin_account, origin_version, decision_round_id)
    if any(not isinstance(x,str) or not x.strip() for x in required):
        raise ValueError("complete source/version/round identity required")
    if not source_signal_id and not shadow_event_key:
        raise ValueError("original confirmation identity required")
    for identity in (source_signal_id,shadow_event_key):
        if identity is not None and (isinstance(identity,bool) or
                not isinstance(identity,(str,int)) or not str(identity).strip()):
            raise ValueError("invalid confirmation identity")
    if origin_account_id is not None and (type(origin_account_id) is not int or origin_account_id <= 0):
        raise ValueError("origin_account_id must be a positive integer")
    if portfolio_version is not None and (not isinstance(portfolio_version,str) or not portfolio_version.strip()):
        raise ValueError("portfolio_version required when supplied")
    # New consumers must supply both fields. Optionality preserves legacy callers,
    # never authorizes unknown account provenance for actual portfolio execution.
    payload = [*required, str(source_signal_id) if source_signal_id is not None else None,
               str(shadow_event_key) if shadow_event_key is not None else None,
               origin_account_id, portfolio_version]
    return "ps1:" + hashlib.sha256(json.dumps(payload,ensure_ascii=False,
                              separators=(",",":")).encode()).hexdigest()


def _lot_fee(price, commission_rate, min_commission):
    """Upper-bound fee of the smallest legal BUY slice, rounded upward."""
    px, rate, minimum = map(_number, (price, commission_rate, min_commission))
    if px <= 0 or rate < Decimal(".0003") or minimum < 5:
        raise ValueError("invalid conservative fee inputs")
    try:
        result = max(minimum, px*100*rate).quantize(Decimal(".01"), rounding=ROUND_CEILING)
        if not math.isfinite(float(result)):
            raise ValueError("non-finite fee")
        return result
    except (InvalidOperation, OverflowError) as exc:
        raise ValueError("fee overflow") from exc


def worst_case_buy_fee(*, quantity, price, commission_rate, min_commission):
    """Reserve, never debit: at most quantity/100 independently charged slices.

    Real commissions remain broker/book receipts, often lower than this bound.
    No cancellation/partial remainder may continue reserving already paid fees.
    """
    if type(quantity) is not int or quantity <= 0 or quantity % 100:
        raise ValueError("BUY fee reservation requires positive whole 100-share lots")
    fee = _lot_fee(price, commission_rate, min_commission) * (quantity // 100)
    result = float(fee)
    if not math.isfinite(result):
        raise ValueError("non-finite fee")
    return result


def allocate_budget(*, policy, total_assets, cash, reserved_cash, held_exposure,
                    pending_exposure, symbol_held_exposure, symbol_pending_exposure,
                    occupied_symbols, daily_new_symbols, is_new_symbol, price,
                    route_cap_ratio):
    """Whole-lot BUY capacity only, never a fill or risk approval.

    occupied_symbols is the DISTINCT held+pending union; daily_new_symbols is
    today's distinct opened+pending new names (caller must not double count).
    Existing symbols still require caller's origin/version/add-on risk checks.
    The stricter original route cap is mandatory; unknown caps do not default.
    """
    error = policy_error(policy)
    if error:
        return BudgetDecision(False,error)
    try:
        assets, cash_d, reserved, held, pending, symbol_held, symbol_pending, px, route = (
            _number(x) for x in (total_assets,cash,reserved_cash,held_exposure,
                                 pending_exposure,symbol_held_exposure,
                                 symbol_pending_exposure,price,route_cap_ratio))
        if assets <= 0 or px <= 0 or not 0 < route <= 1:
            raise ValueError("invalid assets/price/cap")
        if any(type(x) is not int or x < 0 for x in (occupied_symbols,daily_new_symbols)):
            raise ValueError("unknown counts")
        if type(is_new_symbol) is not bool:
            raise ValueError("unknown symbol classification")
        if (symbol_held > held or symbol_pending > pending or cash_d > assets or
                reserved > cash_d or pending > reserved or held > assets):
            raise ValueError("inconsistent wallet aggregates")
        if is_new_symbol != (symbol_held == 0 and symbol_pending == 0):
            raise ValueError("inconsistent new symbol")
        if occupied_symbols == 0 and held+pending > 0:
            raise ValueError("inconsistent occupied count")
    except (ValueError,TypeError,OverflowError):
        return BudgetDecision(False,"invalid_budget_input")
    if occupied_symbols > policy.max_symbols or (is_new_symbol and occupied_symbols >= policy.max_symbols):
        return BudgetDecision(False,"max_symbols")
    if is_new_symbol and daily_new_symbols >= policy.max_daily_new_symbols:
        return BudgetDecision(False,"max_daily_new_symbols")
    # Other pending BUY fees are future asset loss, not existing inventory.
    # They must be charged once to the prospective denominator, never to cash.
    other_fees = reserved-pending
    assets_after_other_fees = assets-other_fees
    if assets_after_other_fees <= 0:
        return BudgetDecision(False,"assets_exhausted_by_pending_fees")
    portfolio_cap = _number(policy.max_exposure_ratio)
    symbol_cap = min(_number(policy.max_symbol_ratio),route)
    portfolio_room = assets_after_other_fees*portfolio_cap-held-pending
    symbol_room = assets_after_other_fees*symbol_cap-symbol_held-symbol_pending
    if portfolio_room <= 0:
        return BudgetDecision(False,"portfolio_exposure_cap")
    if symbol_room <= 0:
        return BudgetDecision(False,"symbol_exposure_cap")
    try:
        lot_fee = _lot_fee(px,policy.commission_rate,policy.min_commission)
        lot_value = px*100
        # N <= cap*(A-F_other-F_new)-existing_exposure; F_new=lots*lot_fee.
        capacities = (
            (int((cash_d-reserved)//(lot_value+lot_fee)), "cash_with_slice_fees_below_one_lot"),
            (int(portfolio_room//(lot_value+portfolio_cap*lot_fee)), "portfolio_post_fee_whole_lot_limit"),
            (int(symbol_room//(lot_value+symbol_cap*lot_fee)), "symbol_post_fee_whole_lot_limit"),
        )
        lots, limiting_reason = min(capacities,key=lambda item:item[0])
        if lots <= 0:
            return BudgetDecision(False,limiting_reason)
        amount = lots*100
        notional, fee = lot_value*lots, lot_fee*lots
        if not all(math.isfinite(float(v)) for v in (notional,fee,notional+fee)):
            return BudgetDecision(False,"invalid_budget_input")
    except (ValueError,InvalidOperation,OverflowError):
        return BudgetDecision(False,"invalid_budget_input")
    return BudgetDecision(True,"budget_available",amount,float(notional),float(fee),float(notional+fee))
