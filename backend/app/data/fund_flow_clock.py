"""Fund-source clocks: provider quote time is not the HTTP receipt time.

Eastmoney f124 is a quote-update watermark (Unix seconds), not a separately
verified funds-calculation timestamp. Legacy rows without it stay unknown.
"""
from datetime import date, datetime
from numbers import Real
from zoneinfo import ZoneInfo
import math

from numpy import bool_

from app.core.trade_calendar import trading_elapsed_seconds


_MARKET_TZ = ZoneInfo("Asia/Shanghai")


def local_clock(value) -> datetime | None:
    """Normalize actual datetime values only; never infer dates or replace NaT."""
    if not isinstance(value, datetime):
        return None
    try:
        if value != value:  # pandas.NaT
            return None
        if value.tzinfo is not None:
            value = value.astimezone(_MARKET_TZ).replace(tzinfo=None)
        return value
    except (ValueError, TypeError, OverflowError):
        return None


def eastmoney_quote_clock(value) -> datetime | None:
    """Decode f124 as seconds, never guess milliseconds or a local timezone."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        if not value.isascii() or not value.isdigit():
            return None
        try:
            value = int(value)
        except (ValueError, TypeError, OverflowError):
            return None
    if not isinstance(value, Real):
        return None
    try:
        if not math.isfinite(value) or value <= 0 or value != int(value):
            return None
        return datetime.fromtimestamp(value, _MARKET_TZ).replace(tzinfo=None)
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def verified_fund_clocks(source_at, received_at, observed_at, trade_date: date,
                         max_age_seconds: float) -> tuple[datetime, datetime, datetime] | None:
    """Require same-day source <= receipt <= observation, fresh at observation."""
    source_at, received_at, observed_at = (
        local_clock(value) for value in (source_at, received_at, observed_at)
    )
    if any(value is None for value in (source_at, received_at, observed_at)):
        return None
    try:
        limit = float(max_age_seconds)
        if not math.isfinite(limit) or limit <= 0:
            return None
        if not all(value.date() == trade_date for value in (source_at, received_at, observed_at)):
            return None
        if not source_at <= received_at <= observed_at:
            return None
        # 新鲜度按**交易时间**度量，不按墙钟：午休（11:30–13:00）不推进交易时钟，
        # 否则正常的午休休市会被误判成数据陈旧。交易时段内的真实缺口仍照常拦截。
        if (
            trading_elapsed_seconds(observed_at) - trading_elapsed_seconds(source_at)
        ) > limit:
            return None
    except (ValueError, TypeError, OverflowError):
        return None
    return source_at, received_at, observed_at


def main_fund_values_valid(amount, percentage) -> bool:
    """Both main-order values must be real; zero is valid, missing is not zero."""
    if isinstance(amount, (bool, bool_)) or isinstance(percentage, (bool, bool_)):
        return False
    try:
        return math.isfinite(float(amount)) and math.isfinite(float(percentage))
    except (ValueError, TypeError, OverflowError):
        return False


def evidence_clock(value) -> datetime | None:
    """Decode a stored ISO clock or datetime, without inventing a missing date."""
    if isinstance(value, str):
        # A date-only string is not an observed midnight timestamp.
        if len(value) < 19 or value[10] not in {"T", " "}:
            return None
        try:
            value = datetime.fromisoformat(value)
        except (ValueError, TypeError):
            return None
    return local_clock(value)


def fund_clock_status(source_at, received_at, observed_at, trade_date: date,
                      decision_at, max_age_seconds: float, *, require_live: bool = True) -> str:
    """Validate original provenance and first availability at the decision cutoff.

    Historical daily values may remain research inputs; that does not make them
    current intraday evidence. No consumer can repair a missing source clock.
    """
    source_at, received_at, observed_at, decision_at = (
        evidence_clock(value) for value in (source_at, received_at, observed_at, decision_at)
    )
    if any(value is None for value in (source_at, received_at, observed_at, decision_at)):
        return "unknown"
    if observed_at > decision_at or source_at > decision_at or received_at > decision_at:
        return "future"
    if not verified_fund_clocks(source_at, received_at, observed_at, trade_date, max_age_seconds):
        return "invalid"
    if require_live and not verified_fund_clocks(
        source_at, observed_at, decision_at, trade_date, max_age_seconds,
    ):
        return "stale"
    return "ok" if require_live else "historical_known"
