"""Tencent hsfundtab mapping, kept separate from quote field 50.

2026-09-09 response contract: main = super + big (>=20万元 or >=6万股).
Amounts are yuan. Classified in/out count both sides of a matched trade;
turnover = (mainIn + mainOut + retailIn + retailOut) / 2.
Neither integer-rounded mainInRate nor quote/order-book fields are net ratios.
"""
from datetime import datetime, time
from decimal import Decimal, InvalidOperation
import re

from app.data.fund_flow_clock import local_clock, verified_fund_clocks

SOURCE_VERSION = "tencent_hsfundtab_v1"
FUND_URL = "https://proxy.finance.qq.com/cgi/cgi-bin/fundflow/hsfundtab"
# Supplier integer-yuan rounding produces differences of 0/1 yuan in verified
# samples (including >9bn turnover). Never use a relative-% tolerance for money.
ROUNDING_YUAN = Decimal(2)
MAX_MONEY = Decimal("1000000000000000")
AMOUNTS = (
    "mainNetIn", "mainIn", "mainOut", "retailIn", "retailOut",
    "superFlow", "bigFlow", "normalFlow", "smallFlow",
)
TREND_FIELDS = {
    "mainNetIn": "MainNetInflow", "mainIn": "MainInflow", "mainOut": "MainOutflow",
    "superFlow": "SuperNetInflow", "bigFlow": "BigNetInflow",
    "normalFlow": "NormalNetInflow", "smallFlow": "SmallNetInflow",
}


def tencent_fund_symbol(code: str) -> str:
    if not isinstance(code, str) or not re.fullmatch(r"[034689][0-9]{5}", code):
        raise ValueError("invalid_fund_code")
    prefix = "sh" if code.startswith("6") else "bj" if code[0] in "489" else "sz"
    return prefix + code


def _money(value) -> Decimal:
    # The measured endpoint serializes integer yuan, not 万/亿/% or floats.
    # Bound text before Decimal/int conversion; reject bool, NaN and exponent tricks.
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError("invalid_fund_number")
    raw = str(value)
    if len(raw) > 18 or not re.fullmatch(r"[+-]?[0-9]+", raw):
        raise ValueError("invalid_fund_number")
    try:
        result = Decimal(raw)
    except InvalidOperation:
        raise ValueError("invalid_fund_number") from None
    if abs(result) > MAX_MONEY:
        raise ValueError("fund_number_out_of_range")
    return result


def _minute(raw) -> datetime:
    if not isinstance(raw, str) or not re.fullmatch(r"[0-9]{12}", raw):
        raise ValueError("invalid_fund_minute")
    try:
        value = datetime.strptime(raw, "%Y%m%d%H%M")
    except ValueError:
        raise ValueError("invalid_fund_minute") from None
    if not (time(9, 30) <= value.time() <= time(11, 30)
            or time(13, 0) <= value.time() <= time(15, 0)):
        raise ValueError("fund_minute_outside_session")
    return value


def parse_tencent_fund_payload(payload, *, code: str, received_at: datetime,
                               max_age_seconds: float) -> dict:
    """Return one owned frame row or fail closed; never write/backfill a minute.

    Only the latest source minute is considered. An old point cannot lend its
    clock to today's summary. Source <= receipt is checked before any value
    enters FundFlow. The minute is a provider watermark, not a transaction clock.
    """
    symbol = tencent_fund_symbol(code)
    receipt = local_clock(received_at)
    if receipt is None:
        raise ValueError("invalid_receipt")
    if not isinstance(payload, dict) or type(payload.get("code")) is not int or payload["code"] != 0:
        raise ValueError("fund_business_error")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ValueError("missing_fund_data")
    flow, trend = data.get("todayFundFlow"), data.get("todayFundTrend")
    if not isinstance(flow, dict) or not isinstance(trend, dict):
        raise ValueError("missing_fund_summary_or_trend")
    if flow.get("stockCode") != symbol or trend.get("stockCode") != symbol:
        raise ValueError("fund_identity_mismatch")
    desc = flow.get("desc")
    if (not isinstance(desc, str) or "主力=超大单+大单" not in desc
            or "成交金额大于等于20万元或者大于等于6万股" not in desc):
        raise ValueError("fund_definition_changed")
    points = trend.get("minList")
    if not isinstance(points, list) or not 1 <= len(points) <= 242:
        raise ValueError("missing_or_excessive_fund_minutes")
    prior = None
    for point in points:
        if not isinstance(point, dict):
            raise ValueError("invalid_fund_point")
        source_at = _minute(point.get("time"))
        if source_at.date() != receipt.date() or (prior is not None and source_at <= prior):
            raise ValueError("fund_minutes_cross_date_duplicate_or_unsorted")
        prior = source_at
    source_at = prior
    if verified_fund_clocks(source_at, receipt, receipt, receipt.date(), max_age_seconds) is None:
        raise ValueError("fund_minute_stale_or_future")
    last = points[-1]
    amounts = {key: _money(flow.get(key)) for key in AMOUNTS}
    for key, trend_key in TREND_FIELDS.items():
        # Same response, same numerical snapshot. Rounding is allowed only in
        # accounting sums below; differing summary/trend values have no shared clock.
        if amounts[key] != _money(last.get(trend_key)):
            raise ValueError("fund_summary_trend_mismatch")
    retail_net = _money(last.get("RetailNetInflow"))
    inflow = amounts["mainIn"] + amounts["retailIn"]
    outflow = amounts["mainOut"] + amounts["retailOut"]
    if any(amounts[key] < 0 for key in ("mainIn", "mainOut", "retailIn", "retailOut")):
        raise ValueError("negative_gross_funds")
    identities = (
        amounts["mainNetIn"] - amounts["mainIn"] + amounts["mainOut"],
        amounts["mainNetIn"] - amounts["superFlow"] - amounts["bigFlow"],
        inflow - outflow,
        retail_net - amounts["retailIn"] + amounts["retailOut"],
        retail_net - amounts["normalFlow"] - amounts["smallFlow"],
        retail_net + amounts["mainNetIn"],
    )
    if any(abs(error) > ROUNDING_YUAN for error in identities):
        raise ValueError("fund_accounting_mismatch")
    turnover = (inflow + outflow) / 2
    if turnover <= 0 or turnover > MAX_MONEY:
        raise ValueError("missing_or_invalid_fund_turnover")
    row = {"代码": code, "名称": code, "source_quote_at": source_at, "received_at": receipt}
    for key, label in (("mainNetIn", "主力"), ("superFlow", "超大单"),
                       ("bigFlow", "大单"), ("normalFlow", "中单"), ("smallFlow", "小单")):
        amount = amounts[key]
        ratio = amount / turnover * 100
        if abs(ratio) > 100:
            raise ValueError("fund_ratio_out_of_range")
        row[f"{label}净流入-净额"] = float(amount)
        row[f"{label}净流入-净占比"] = float(ratio)
    return row
