"""Bounded official and Tencent/Sina fixed-price aggregates; research only.

Validated sample API mappings (2026-10-02), not an SLA/full-universe/ETF claim.
Closing quote observed after matching is NOT certified available at 15:00.
"""
import asyncio
from datetime import date, datetime, time
from decimal import Decimal
import hashlib
import json
import re
from zoneinfo import ZoneInfo

import httpx

from app.data.after_hours import _number, TENCENT_VERSION, SINA_VERSION, SUPPLIER_DAILY_BASIS

VERSION = "official_after_hours_fields_20261002_v1"
RULE_VERSION = "sse41_szse551_2026_effective_20260706"
MAX_BYTES = 256 * 1024
SHANGHAI = ZoneInfo("Asia/Shanghai")


def local_now():
    return datetime.now(SHANGHAI).replace(tzinfo=None)


def exchange_of(code):
    if not isinstance(code, str) or re.fullmatch(r"[0-9]{6}", code) is None:
        raise ValueError("invalid security identity")
    if code.startswith(("600", "601", "603", "605", "688", "689")):
        return "SSE"
    if code.startswith(("000", "001", "002", "003", "300", "301")):
        return "SZSE"
    raise ValueError("unsupported research security (A-shares only; ETF/BSE not certified)")


def source_url(code):
    if exchange_of(code) == "SSE":
        return (f"https://yunhq.sse.com.cn:32042/v1/sh1/snap/{code}"
                "?select=name,last,volume,amount,fp_volume,fp_amount,fp_phase")
    return f"https://www.szse.cn/api/market/ssjjhq/getTimeData?marketId=1&code={code}"


def parse_official(payload, *, code, day, received_at):
    """Return explicit measured fields; don't forward-fill, difference or fabricate zero."""
    exchange = exchange_of(code)
    if type(day) is not date or day < date(2026, 7, 6) or day > received_at.date():
        raise ValueError("unsupported/future trade date")
    if received_at.tzinfo is not None:
        raise ValueError("clocks must be naive Shanghai")
    if not isinstance(payload, dict):
        raise ValueError("invalid response")
    if exchange == "SSE":
        if payload.get("code") != code or not isinstance(payload.get("snap"), list) or len(payload["snap"]) != 7:
            raise ValueError("source identity/shape mismatch")
        at = datetime.strptime(str(payload.get("date")) + str(payload.get("time")).zfill(6), "%Y%m%d%H%M%S")
        _, price, volume, amount, post_volume, post_amount, phase = payload["snap"]
        closed = isinstance(phase, str) and phase.startswith("D")
        multiplier = 1
        units = {"volume": "shares", "amount": "CNY", "fp_volume": "shares", "fp_amount": "CNY"}
    else:
        data = payload.get("data")
        if payload.get("code") != "0" or not isinstance(data, dict) or data.get("code") != code:
            raise ValueError("source identity/status mismatch")
        at = datetime.strptime(data.get("marketTime", ""), "%Y-%m-%d %H:%M:%S")
        # 'close' here is the PREVIOUS close. 'now' is the current last price.
        price, volume, amount = data.get("now"), data.get("volume"), data.get("amount")
        post_volume, post_amount, phase = data.get("volumeAhT"), data.get("amountAhT"), data.get("tradingPhaseCode2")
        closed = phase == "00"
        multiplier = 100
        units = {"volume": "hands", "amount": "CNY", "volumeAhT": "hands", "amountAhT": "CNY"}
    if at.date() != day or at > received_at:
        raise ValueError("wrong/future source date")
    pv, total = _number(post_volume, integer=True), _number(volume, integer=True)
    pv = pv * multiplier if pv is not None else None
    total = total * multiplier if total is not None else None
    values = {
        "close_price_1500": _number(price, positive=True) if closed and at.time() >= time(15, 30) else None,
        "price_basis": "official_unadjusted_last_after_fixed_price_session",
        "after_volume_shares": pv, "after_amount_yuan": _number(post_amount),
        "reported_daily_volume": total, "reported_daily_amount": _number(amount),
        "reported_daily_volume_basis": "source_daily_cumulative",
        "reported_daily_amount_basis": "source_daily_cumulative",
        "source_units": units, "session_state": "closed" if closed else "not_closed_or_unknown",
        "source_url": source_url(code), "rule_version": RULE_VERSION,
        "volume_basis": "official_native_shares" if multiplier == 1 else "official_hands_times_100",
        "mapping_scope": "A_share_API_observation_not_full_market_or_ETF_certification",
    }
    # Rules 3.7.10/3.6.10 include post trades in the daily total after session end.
    # Still an observed cumulative value, not proof the source cannot revise it.
    ended = closed and at.time() >= time(15, 30) and received_at >= datetime.combine(day, time(15, 30))
    if ended:
        values.update(all_day_volume_shares=total, all_day_amount_yuan=_number(amount),
                      daily_total_basis="official_daily_total_after_closed_session")
    else:
        values.update(after_volume_shares=None, after_amount_yuan=None,
                      reason="session_not_closed_or_source_before_1530")
    return {"values": values, "source_quote_at": at, "received_at": received_at,
            "source": "sse_fixed_price" if exchange == "SSE" else "szse_fixed_price",
            "source_version": VERSION}


def _supplier_fields(payload, *, code, provider):
    """Decode one exact quote assignment, never evaluate supplier JavaScript."""
    symbol = ("sh" if exchange_of(code) == "SSE" else "sz") + code
    prefix = "v_" if provider == "tencent" else "hq_str_"
    match = re.fullmatch(r'\s*(?:var\s+)?' + prefix + re.escape(symbol)
                         + r'="([^"\r\n]*)";\s*', payload)
    if match is None:
        raise ValueError("supplier response identity/shape mismatch")
    fields = match[1].split("~" if provider == "tencent" else ",")
    if len(fields) < (60 if provider == "tencent" else 34):
        raise ValueError("independent after-hours fields absent")
    if provider == "tencent" and fields[2] != code:
        raise ValueError("supplier security identity mismatch")
    return fields


def _scaled(value, multiplier, *, integer=False):
    if _number(value, integer=integer) is None:
        return None
    return _number(Decimal(str(value)) * multiplier, integer=integer)


def _supplier_material(*, code, day, received_at, source_at, provider, price,
                       volume, amount, post_volume, post_amount, units, volume_basis,
                       phase_closed=True):
    if (type(day) is not date or day < date(2026, 7, 6) or not isinstance(received_at, datetime)
            or received_at.tzinfo is not None or day > received_at.date()):
        raise ValueError("unsupported date or supplier receive clock")
    if source_at.date() != day or source_at > received_at:
        raise ValueError("wrong/future supplier source date")
    ended = (phase_closed and source_at.time() >= time(15, 30)
             and received_at >= datetime.combine(day, time(15, 30)))
    # Tencent [58] is in 10,000 CNY with four-decimal sample precision;
    # Sina's independently reported amount is in CNY with cent sample precision.
    precision = 1.0 if provider == "tencent" else 0.01
    price = _number(price, positive=True)
    if ended and post_volume is not None and post_amount is not None:
        if ((post_volume == 0) != (post_amount == 0)
                or (price is not None and abs(Decimal(str(post_amount))
                    - Decimal(str(price)) * post_volume) > Decimal(str(precision)))):
            raise ValueError("supplier independent volume/amount inconsistent")
    if ended and any(total is not None and part is not None and total < part
                     for total, part in ((volume, post_volume), (amount, post_amount))):
        raise ValueError("supplier daily total less than independent after-hours")
    values = {
        "close_price_1500": price if ended else None,
        "price_basis": "supplier_unadjusted_last_after_session_not_1500_availability",
        "after_volume_shares": post_volume if ended else None,
        "after_amount_yuan": post_amount if ended else None,
        "reported_daily_volume": volume, "reported_daily_amount": amount,
        "reported_daily_volume_basis": "supplier_native_daily_cumulative",
        "reported_daily_amount_basis": "supplier_native_daily_cumulative_not_vwap_estimate",
        "source_units": units, "volume_basis": volume_basis,
        "amount_precision_yuan": precision,
        "session_state": "closed" if ended else "not_closed_or_unknown",
        "session_state_basis": ("source_quote_clock_after_1530_not_exchange_phase_certificate"
                                if provider == "tencent" else "sina_native_D_phase_and_quote_clock"),
        "mapping_evidence": "four_code_official_tencent_sina_cross_check_20261002",
        "mapping_scope": "sample_verified_A_share_mapping_not_full_coverage_or_finality",
        "rule_version": RULE_VERSION, "source_url": supplier_url(code, provider),
    }
    if ended:
        values.update(all_day_volume_shares=volume, all_day_amount_yuan=amount,
                      daily_total_basis=SUPPLIER_DAILY_BASIS)
    else:
        values["reason"] = "supplier_session_not_closed_or_source_before_1530"
    return {"values": values, "source_quote_at": source_at, "received_at": received_at,
            "source": provider + "_after_hours",
            "source_version": TENCENT_VERSION if provider == "tencent" else SINA_VERSION}


def supplier_url(code, provider):
    symbol = ("sh" if exchange_of(code) == "SSE" else "sz") + code
    if provider == "tencent":
        return "https://qt.gtimg.cn/q=" + symbol
    if provider == "sina":
        return "https://hq.sinajs.cn/list=" + symbol
    raise ValueError("unsupported after-hours supplier")


def parse_tencent(payload, *, code, day, received_at):
    fields = _supplier_fields(payload, code=code, provider="tencent")
    at = datetime.strptime(fields[30], "%Y%m%d%H%M%S")
    # Preserve the native STAR shares instead of ordinary spot's rounded hands.
    multiplier = 1 if code.startswith("68") else 100
    unit = "shares" if multiplier == 1 else "hands"
    return _supplier_material(code=code, day=day, received_at=received_at, source_at=at,
        provider="tencent", price=fields[3], volume=_scaled(fields[6], multiplier, integer=True),
        amount=_scaled(fields[57], 10000), post_volume=_scaled(fields[59], multiplier, integer=True),
        post_amount=_scaled(fields[58], 10000),
        units={"6": unit, "57": "10000_CNY", "58": "10000_CNY", "59": unit},
        volume_basis="tencent_native_shares" if multiplier == 1 else "tencent_hands_100_share_precision")


def parse_sina(payload, *, code, day, received_at):
    fields = _supplier_fields(payload, code=code, provider="sina")
    at = datetime.strptime(fields[30] + " " + fields[31], "%Y-%m-%d %H:%M:%S")
    post = fields[33].split("|")
    if len(post) != 3:
        raise ValueError("sina independent after-hours field shape")
    return _supplier_material(code=code, day=day, received_at=received_at, source_at=at,
        provider="sina", price=fields[3], volume=_number(fields[8], integer=True),
        amount=_number(fields[9]), post_volume=_number(post[1], integer=True),
        post_amount=_number(post[2]), phase_closed=post[0] == "D",
        units={"8": "shares", "9": "CNY", "33_volume": "shares", "33_amount": "CNY"},
        volume_basis="sina_native_shares")


class AfterHoursSource:
    """Official single GET or Tencent/Sina failover; caller owns total budget/storage."""
    rate_limit = 0.2

    def __init__(self, *, provider="official"):
        if provider not in {"official", "tencent_sina"}:
            raise ValueError("unsupported after-hours research source mode")
        self.provider = provider

    def identity(self, code):
        if self.provider == "official":
            return {"source": "sse_fixed_price" if exchange_of(code) == "SSE" else "szse_fixed_price",
                    "source_version": VERSION}
        exchange_of(code)
        return {"source": "tencent_sina_after_hours", "source_version": "free_after_hours_failover_v1"}

    async def collect(self, code, *, trade_date, client=None):
        if client is None:
            async with httpx.AsyncClient(timeout=8, follow_redirects=False) as owned:
                return await self.collect(code, trade_date=trade_date, client=owned)
        if self.provider == "tencent_sina":
            return await self._collect_free(code, trade_date=trade_date, client=client)
        url = source_url(code)
        body, received_at = await self._fetch(client, url,
            referer="https://www.sse.com.cn/" if exchange_of(code) == "SSE" else "https://www.szse.cn/")
        material = parse_official(json.loads(body), code=code, day=trade_date, received_at=received_at)
        material["values"]["response_hash"] = hashlib.sha256(body).hexdigest()
        return material

    async def _fetch(self, client, url, *, referer):
        async with client.stream("GET", url, headers={"Referer": referer}) as response:
            response.raise_for_status()
            chunks, size = [], 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > MAX_BYTES:
                    raise ValueError("after-hours response exceeds byte budget")
                chunks.append(chunk)
            body = b"".join(chunks)
            received_at = local_now()
        return body, received_at

    async def _collect_free(self, code, *, trade_date, client):
        attempts, partial = [], None
        for provider, parser in (("tencent", parse_tencent), ("sina", parse_sina)):
            try:
                # Reserve time for the fallback within the existing 8s per-code
                # and scheduler-wide deadlines; cancellation is never retried.
                body, received = await asyncio.wait_for(self._fetch(client, supplier_url(code, provider),
                    referer="https://finance.sina.com.cn/" if provider == "sina"
                            else "https://gu.qq.com/"), timeout=3.5)
                material = parser(body.decode("gb18030"), code=code, day=trade_date, received_at=received)
                material["values"]["response_hash"] = hashlib.sha256(body).hexdigest()
                complete = all(material["values"][key] is not None
                               for key in ("after_volume_shares", "after_amount_yuan"))
                attempts.append({"source": provider + "_after_hours",
                                 "status": "observed" if complete else "partial"})
                if complete:
                    material["values"]["source_attempts"] = attempts
                    return material
                if partial is None:
                    partial = material
            except Exception as exc:
                attempts.append({"source": provider + "_after_hours", "status": "unavailable",
                                 "error_type": type(exc).__name__})
        if partial is None:
            partial = {**self.identity(code), "received_at": local_now(), "source_quote_at": None,
                       "values": {"reason": "free_after_hours_sources_unavailable"}}
        partial["values"]["source_attempts"] = attempts
        return partial
