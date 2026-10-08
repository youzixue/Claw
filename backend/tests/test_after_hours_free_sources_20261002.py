"""Sample-derived field vectors, mocked HTTP and isolated SQLite; no live collection.

Unused quote fields are synthetic blanks. The tested values below were read on
2026-10-02 from Sep-30 closed Tencent/Sina quotes, not historical PIT fixtures.
"""
import asyncio
import hashlib
import json
from datetime import date, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.data.after_hours import (
    SUPPLIER_DAILY_BASIS, observation, append_observations, read_after_hours, research_features,
)
from app.data.sources.after_hours_source import AfterHoursSource, parse_tencent, parse_sina, MAX_BYTES
from app.models.governance import TradeCalendarModel
from app.models.stock import StockTag
from app.trading.paper_after_hours_allocation import matching_capabilities, _verify, EvidenceUnavailable
from test_after_hours_research_20261002 import db, sse

DAY = date(2026, 9, 30)
RECEIVED = datetime(2026, 10, 2, 12, 50)
SAMPLES = {
    "600000": ("9.48", "1474848", "138620.9937", "42.1860", "445", "147484820", "1386209937.000", "44500", "421860.00"),
    "000001": ("11.57", "1045357", "120581.4858", "102.6259", "887", "104535745", "1205814857.640", "88700", "1026259.000"),
    "300750": ("291.11", "296995", "861392.9784", "451.2205", "155", "29699471", "8613929784.250", "15500", "4512205.000"),
    "688256": ("1009.01", "8588771", "869155.5481", "465.3554", "4612", "8588771", "8691555481.000", "4612", "4653554.12"),
}


def quote(provider, code="600000", changes=None):
    price, tq, ta, pa, pq, sq, sa, spq, spa = SAMPLES[code]
    symbol = ("sh" if code.startswith("6") else "sz") + code
    if provider == "tencent":
        fields = [""] * 88
        values = {2: code, 3: price, 6: tq, 30: "20260930161454", 51: "999999",
                  57: ta, 58: pa, 59: pq}
        prefix, separator = "v_", "~"
    else:
        fields = [""] * 34
        values = {3: price, 8: sq, 9: sa, 30: "2026-09-30", 31: "16:30:00",
                  32: "00", 33: "D|" + spq + "|" + spa}
        prefix, separator = "var hq_str_", ","
    values.update(changes or {})
    for index, value in values.items():
        fields[index] = value
    return prefix + symbol + '="' + separator.join(fields) + '";'


def material(provider, code="600000", changes=None, received=RECEIVED):
    parser = parse_tencent if provider == "tencent" else parse_sina
    return parser(quote(provider, code, changes), code=code, day=DAY, received_at=received)


def row(provider, code="600000", changes=None, received=RECEIVED):
    return observation(code=code, day=DAY, stage="after_hours", accepted_at=received,
                       **material(provider, code, changes, received))


@pytest.mark.parametrize("provider", ["tencent", "sina"])
@pytest.mark.parametrize("code", list(SAMPLES))
def test_native_four_board_values_ratios_and_precision_not_vwap(provider, code):
    payload = json.loads(row(provider, code).payload_json)
    result = research_features(None, payload)
    values = payload["values"]
    expected_quantity = int(SAMPLES[code][7])
    expected_amount = float(SAMPLES[code][8])
    assert result["after_volume_shares"] == expected_quantity
    assert result["after_amount_yuan"] == pytest.approx(expected_amount, abs=1, rel=0)
    assert result["all_day_volume_shares"] == values["reported_daily_volume"]
    assert result["all_day_amount_yuan"] == values["reported_daily_amount"]
    assert result["after_amount_ratio"] == pytest.approx(values["after_amount_yuan"] / values["reported_daily_amount"])
    assert result["derived_regular_volume_shares"] == values["reported_daily_volume"] - expected_quantity
    assert result["regular_volume_shares"] is result["regular_amount_yuan"] is None
    assert result["ratio_basis"] == "observed_supplier_native_daily_total_after_session"
    assert result["strength_interpretation"] == "activity_not_directional_order_flow"
    assert result["after_amount_precision_yuan"] == (1 if provider == "tencent" else 0.01)
    assert result["historical_pit"] is result["trading_authority"] is result["source_finality_verified"] is False
    if provider == "tencent" and code == "600000":
        assert result["all_day_amount_yuan"] == 1386209937
        assert result["all_day_volume_shares"] == 147484800  # native 100-share precision
    if provider == "tencent" and code == "688256":
        assert result["all_day_volume_shares"] == 8588771
        assert result["volume_precision"] == "tencent_native_shares"


@pytest.mark.parametrize("value", ["", "-", "-1", "1.5", "NaN", "Infinity", "True", str(2**63)])
def test_tencent_bad_volume_is_unknown_never_fabricated_zero(value):
    payload = json.loads(row("tencent", changes={59: value}).payload_json)
    assert payload["status"] == "partial"
    assert research_features(None, payload)["after_volume_ratio"] is None


@pytest.mark.parametrize("value", ["", "-", "-1", "NaN", "Infinity", "True"])
def test_tencent_bad_amount_is_unknown_never_price_times_volume(value):
    payload = json.loads(row("tencent", changes={58: value}).payload_json)
    assert payload["values"]["after_amount_yuan"] is None
    assert research_features(None, payload)["after_amount_ratio"] is None


@pytest.mark.parametrize("provider,changes", [
    ("tencent", {30: "20260929163000"}), ("tencent", {30: "20261003163000"}),
    ("tencent", {30: "bad"}), ("tencent", {2: "600001"}),
    ("sina", {30: "2026-09-29"}), ("sina", {30: "2026-10-03"}),
    ("sina", {31: "bad"}), ("sina", {33: "44500"}),
])
def test_bad_identity_date_and_clocks_fail_closed(provider, changes):
    with pytest.raises(ValueError):
        material(provider, changes=changes)


@pytest.mark.parametrize("provider,changes", [
    ("tencent", {30: "20260930152959"}),
    ("sina", {31: "15:29:59"}), ("sina", {33: "T|44500|421860.00"}),
    ("sina", {33: "?|44500|421860.00"}),
])
def test_pre_session_or_unknown_phase_cannot_produce_closed_features(provider, changes):
    payload = json.loads(row(provider, changes=changes).payload_json)
    assert payload["status"] == "partial"
    assert research_features(None, payload)["after_volume_ratio"] is None
    assert payload["values"]["daily_total_basis"] is None


@pytest.mark.parametrize("provider,changes", [
    ("tencent", {59: "0", 58: "0"}), ("sina", {33: "D|0|0"}),
])
def test_explicit_native_zero_remains_zero(provider, changes):
    payload = json.loads(row(provider, changes=changes).payload_json)
    assert payload["status"] == "observed"
    assert research_features(None, payload)["after_volume_ratio"] == 0


@pytest.mark.parametrize("provider,changes", [
    ("tencent", {59: "0"}), ("tencent", {58: "42.1862"}), ("tencent", {6: "1"}),
    ("sina", {33: "D|0|421860.00"}), ("sina", {33: "D|44500|421862"}),
    ("sina", {8: "1"}), ("sina", {9: "1"}),
])
def test_inconsistent_independent_fields_and_totals_are_rejected(provider, changes):
    with pytest.raises(ValueError):
        material(provider, changes=changes)


def test_missing_native_total_amount_does_not_fall_back_to_vwap():
    result = research_features(None, json.loads(row("tencent", changes={57: ""}).payload_json))
    assert result["after_amount_yuan"] == 421860
    assert result["all_day_amount_yuan"] is result["after_amount_ratio"] is None
    assert result["after_volume_ratio"] is not None


def test_unclassified_source_cannot_claim_supplier_daily_basis():
    supplied = material("tencent")
    supplied.update(source="ths_after_hours", source_version="fixture")
    payload = json.loads(observation(code="600000", day=DAY, stage="after_hours",
                                    accepted_at=RECEIVED, **supplied).payload_json)
    assert "unverified_daily_total_basis" in payload["missing"]


def test_all_research_sources_remain_unable_to_authorize_matching():
    assert matching_capabilities()["order_level_provider_count"] == 0
    for provider in ("tencent", "sina"):
        with pytest.raises(EvidenceUnavailable, match="provider_not_audited"):
            _verify({"source": provider + "_after_hours"}, now=datetime(2026, 9, 30, 15, 10))


@pytest.mark.asyncio
@pytest.mark.parametrize("first", ["valid", "missing", "oversized", "timeout"])
async def test_bounded_http_primary_and_explicit_fallback(monkeypatch, first):
    monkeypatch.setattr("app.data.sources.after_hours_source.local_now", lambda: RECEIVED)
    calls = []
    async def fixture(request):
        calls.append(request)
        if request.url.host == "qt.gtimg.cn":
            if first == "timeout":
                raise httpx.ConnectTimeout("fixture")
            if first == "oversized":
                return httpx.Response(200, content=b"x" * (MAX_BYTES + 1))
            body = quote("tencent", changes={58: ""} if first == "missing" else None)
        else:
            assert request.headers["Referer"] == "https://finance.sina.com.cn/"
            body = quote("sina")
        return httpx.Response(200, content=body.encode("gb18030"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(fixture), trust_env=False) as client:
        result = await AfterHoursSource(provider="tencent_sina").collect("600000", trade_date=DAY, client=client)
    assert len(calls) == (1 if first == "valid" else 2)
    assert result["source"] == ("tencent_after_hours" if first == "valid" else "sina_after_hours")
    body = quote("tencent" if first == "valid" else "sina").encode("gb18030")
    assert result["values"]["response_hash"] == hashlib.sha256(body).hexdigest()
    assert result["values"]["source_attempts"][0]["source"] == "tencent_after_hours"


@pytest.mark.asyncio
async def test_both_sources_unavailable_return_explicit_unknown(monkeypatch):
    monkeypatch.setattr("app.data.sources.after_hours_source.local_now", lambda: RECEIVED)
    async def fixture(request):
        return httpx.Response(403)
    async with httpx.AsyncClient(transport=httpx.MockTransport(fixture), trust_env=False) as client:
        result = await AfterHoursSource(provider="tencent_sina").collect("600000", trade_date=DAY, client=client)
    payload = json.loads(observation(code="600000", day=DAY, stage="after_hours",
                                    accepted_at=RECEIVED, **result).payload_json)
    assert payload["status"] == "partial"
    assert payload["values"]["after_volume_shares"] is None
    assert len(payload["values"]["source_attempts"]) == 2


@pytest.mark.asyncio
async def test_outer_cancellation_does_not_start_fallback():
    calls = []
    async def fixture(request):
        calls.append(request)
        raise asyncio.CancelledError()
    async with httpx.AsyncClient(transport=httpx.MockTransport(fixture), trust_env=False) as client:
        with pytest.raises(asyncio.CancelledError):
            await AfterHoursSource(provider="tencent_sina").collect("600000", trade_date=DAY, client=client)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_late_read_never_backdates_and_idempotent_recheck_retains_clock(db):
    await append_observations(db, [row("sina")])
    await db.commit()
    assert (await read_after_hours(db, day=DAY, as_of=datetime(2026, 9, 30, 20, 35)))["items"] == []
    later = RECEIVED + timedelta(hours=1)
    assert (await append_observations(db, [row("sina", received=later)]))["inserted"] == 0
    await db.commit()
    item = (await read_after_hours(db, day=DAY, as_of=later))["items"][0]
    assert item["source_refs"][0]["available_at"] == RECEIVED.isoformat()
    assert item["source_refs"][0]["source"] == "sina_after_hours"
    assert item["after_amount_yuan"] == 421860
    assert item["trading_authority"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["tencent_sina", "official"])
async def test_existing_scheduler_selects_source_without_new_jobs(monkeypatch, db, mode):
    from app.data import scheduler as module
    import app.data.sources.after_hours_source as adapter
    now = datetime(2026, 9, 30, 16, 30)
    monkeypatch.setattr(adapter, "local_now", lambda: now)
    monkeypatch.setattr(module.settings, "AFTER_HOURS_RESEARCH_SOURCE", mode)
    monkeypatch.setattr(module.settings, "AFTER_HOURS_RESEARCH_ENABLED", True)
    db.add(TradeCalendarModel(trade_date=DAY, is_trade_day=True, session_type="full"))
    db.add(StockTag(code="600000", name="研究样本", board_type="main_sh", board_tag="tradeable", is_suspended=False))
    await db.commit()
    monkeypatch.setattr(module, "async_session", async_sessionmaker(db.bind, expire_on_commit=False))
    real_client = httpx.AsyncClient
    calls = []
    async def fixture(request):
        calls.append(request.url.host)
        return (httpx.Response(200, content=quote("tencent").encode("gb18030")) if mode == "tencent_sina"
                else httpx.Response(200, json=sse()))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real_client(
        **kwargs, trust_env=False, transport=httpx.MockTransport(fixture)))
    service = module.DataScheduler()
    result = await service._collect_after_hours_research()
    assert result["status_counts"] == {"observed": 1}
    assert calls == (["qt.gtimg.cn"] if mode == "tencent_sina" else ["yunhq.sse.com.cn"])
    item = (await read_after_hours(db, day=DAY, as_of=now))["items"][0]
    assert item["after_volume_shares"] == 44500
    assert item["source_refs"][0]["source"] == ("tencent_after_hours" if mode == "tencent_sina" else "sse_fixed_price")
    assert result["trading_authority"] is False


@pytest.mark.asyncio
async def test_next_trading_day_context_reads_supplier_ratios_across_holiday(monkeypatch, db):
    from app.review.overnight_evidence import read_premarket_context
    from app.models.stock import StockKline
    target, cutoff = date(2026, 10, 8), datetime(2026, 10, 8, 8)
    current = DAY
    while current <= target:
        db.add(TradeCalendarModel(trade_date=current, is_trade_day=current in {DAY, target}, session_type="full"))
        current += timedelta(days=1)
    db.add(StockKline(code="600000", trade_date=DAY, close=9.48, source="ths", volume=1000))
    await append_observations(db, [row("tencent")])
    await db.commit()
    # Future date/cutoff belongs only to this isolated stored-calendar fixture.
    result = await read_premarket_context(db, trade_date=target, as_of=cutoff)
    item = result["after_hours"]["items"][0]
    assert item["after_amount_yuan"] == 421860
    assert item["after_amount_ratio"] == pytest.approx(421860 / 1386209937)
    assert result["research_fusion"]["statistics"]["dimensions"]["after_hours"]["statistics"]["after_amount_ratio"]["valid_count"] == 1
    assert result["research_fusion"]["automatic_weight_update"] is False
