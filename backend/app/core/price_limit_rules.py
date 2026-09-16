"""Date-effective A-share price-limit rules.

All prediction, labelling and review code must use this module rather than
embedding board/ST thresholds.  Thresholds are deliberately tolerant of
vendor rounding and forward-adjusted daily bars while the nominal limit is
kept separately for audit output.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


ST_MAINBOARD_LIMIT_REFORM_DATE = date(2026, 7, 6)
GEM_LIMIT_REFORM_DATE = date(2020, 8, 24)


@dataclass(frozen=True, slots=True)
class PriceLimitRule:
    board: str
    nominal_limit_pct: float
    detection_threshold_pct: float
    is_st: bool
    effective_date: date | None
    rule_version: str = "a_share_price_limit_v2"


def _parse_date(value: date | str | None) -> date | None:
    if isinstance(value, date):
        return value
    if value:
        try:
            return date.fromisoformat(str(value)[:10])
        except ValueError:
            return None
    return None


def board_for_code(code: str) -> str:
    normalized = str(code or "").strip()
    if normalized.startswith(("300", "301")):
        return "gem"
    if normalized.startswith(("688", "689")):
        return "star"
    if normalized.startswith(("4", "8", "92")):
        return "bse"
    return "main"


def is_st_name(name: str | None) -> bool:
    normalized = str(name or "").upper().replace(" ", "")
    return "ST" in normalized or "退" in normalized


def price_limit_rule(
    code: str,
    *,
    name: str | None = None,
    trade_date: date | str | None = None,
    adjusted_bar: bool = False,
) -> PriceLimitRule:
    """Return the applicable nominal limit and a vendor-tolerant threshold."""

    board = board_for_code(code)
    parsed_date = _parse_date(trade_date)
    st = is_st_name(name)

    if st:
        # From 2026-07-06 risk-warning shares use the board's normal limit.
        # The current research universe primarily uses main-board ST rows, but
        # retaining the board limit makes the rule correct for GEM/STAR too.
        if parsed_date and parsed_date >= ST_MAINBOARD_LIMIT_REFORM_DATE:
            nominal = {"gem": 20.0, "star": 20.0, "bse": 30.0}.get(board, 10.0)
            effective = ST_MAINBOARD_LIMIT_REFORM_DATE
        else:
            nominal = 5.0
            effective = None
    else:
        # 创业板注册制首批企业于 2020-08-24 上市，同日起存量股票
        # 涨跌幅限制由 10% 调整为 20%。历史回放必须按交易日切换，不能用
        # 当前 20% 规则反标旧样本。
        if board == "gem" and parsed_date and parsed_date < GEM_LIMIT_REFORM_DATE:
            nominal = 10.0
            effective = None
        else:
            nominal = {"gem": 20.0, "star": 20.0, "bse": 30.0}.get(board, 10.0)
            effective = GEM_LIMIT_REFORM_DATE if board == "gem" else None

    if adjusted_bar:
        threshold = {5.0: 4.5, 10.0: 8.8, 20.0: 18.8, 30.0: 28.8}[nominal]
    else:
        threshold = {5.0: 4.7, 10.0: 9.5, 20.0: 19.0, 30.0: 29.0}[nominal]

    return PriceLimitRule(
        board=board,
        nominal_limit_pct=nominal,
        detection_threshold_pct=threshold,
        is_st=st,
        effective_date=effective,
    )


def limit_up_change_threshold(
    code: str,
    name: str | None = None,
    trade_date: date | str | None = None,
    *,
    adjusted_bar: bool = False,
) -> float:
    return price_limit_rule(
        code,
        name=name,
        trade_date=trade_date,
        adjusted_bar=adjusted_bar,
    ).detection_threshold_pct


def is_limit_up_change(
    code: str,
    change_pct: float,
    *,
    name: str | None = None,
    trade_date: date | str | None = None,
    adjusted_bar: bool = False,
) -> bool:
    try:
        value = float(change_pct)
    except (TypeError, ValueError):
        return False
    return value >= limit_up_change_threshold(
        code,
        name,
        trade_date,
        adjusted_bar=adjusted_bar,
    )
