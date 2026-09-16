"""Isolated Tencent funds contract/clock/numeric/transport tests; no live HTTP/DB."""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import httpx
import pytest

from app.config.settings import settings
from app.data.sources import tencent_source as module
from app.data.sources.tencent_source import TencentSource
from app.data.sources.tencent_fund_flow import (
    SOURCE_VERSION, parse_tencent_fund_payload, tencent_fund_symbol,
)

NOW = datetime(2026, 9, 9, 10, 0, 30)
DESC = "逐笔统计当日成交买卖单，主力=超大单+大单。成交金额大于等于20万元或者大于等于6万股。"


def payload(symbol="sz000001"):
    flow = dict(stockCode=symbol, desc=DESC, mainNetIn="100", mainIn="600",
                mainOut="500", retailIn="400", retailOut="500",
                mainInRate="30", mainOutRate="25", superFlow="60", bigFlow="40",
                normalFlow="-80", smallFlow="-20")
    point = dict(time="202609091000", MainNetInflow="100", MainInflow="600",
                 MainOutflow="500", RetailNetInflow="-100", SuperNetInflow="60",
                 BigNetInflow="40", NormalNetInflow="-80", SmallNetInflow="-20")
    return {"code": 0, "data": {"todayFundFlow": flow,
                              "todayFundTrend": {"stockCode": symbol, "minList": [point]}}}


def parse(body=None, **kwargs):
    return parse_tencent_fund_payload(
        payload() if body is None else body, code="000001",
        received_at=kwargs.pop("received_at", NOW), max_age_seconds=600, **kwargs,
    )


def test_verified_formula_is_net_over_single_sided_turnover_not_buy_rate_or_double_denominator():
    row = parse()
    assert row["主力净流入-净额"] == 100
    assert row["主力净流入-净占比"] == 10  # NOT mainInRate=30 or 30-25=5
    assert row["大单净流入-净占比"] == 4
    assert row["中单净流入-净占比"] == -8
    assert row["source_quote_at"] == NOW.replace(second=0)
    assert row["received_at"] == NOW


def test_real_zero_main_flow_is_retained_when_turnover_exists():
    body = payload()
    f = body["data"]["todayFundFlow"]
    p = body["data"]["todayFundTrend"]["minList"][0]
    for key in ("mainNetIn", "superFlow", "bigFlow", "normalFlow", "smallFlow"):
        f[key] = "0"
    f.update(mainIn="500", mainOut="500", retailIn="500", retailOut="500")
    p.update(MainNetInflow="0", MainInflow="500", MainOutflow="500",
             RetailNetInflow="0", SuperNetInflow="0", BigNetInflow="0",
             NormalNetInflow="0", SmallNetInflow="0")
    assert parse(body)["主力净流入-净占比"] == 0


@pytest.mark.parametrize("bad", [None, True, False, float("nan"), float("inf"),
    0.0, "NaN", "Infinity", "1e4", "1亿", "30%", "１２３", "", " 100", "1.0",
    "1000000000000001", "1" * 10000, 10 ** 5000], ids=lambda value: "malformed")
def test_invalid_or_oversized_money_is_not_coerced(bad):
    body = payload()
    body["data"]["todayFundFlow"]["mainNetIn"] = bad
    with pytest.raises(ValueError):
        parse(body)


@pytest.mark.parametrize("key", ["mainNetIn", "mainIn", "mainOut", "retailIn", "retailOut",
                                 "superFlow", "bigFlow", "normalFlow", "smallFlow"])
def test_missing_measured_component_rejects_without_zero_fill(key):
    body = payload()
    del body["data"]["todayFundFlow"][key]
    with pytest.raises(ValueError):
        parse(body)


@pytest.mark.parametrize("bad", ["20260908", "2026090909", "202609091600",
    "202609091200", "202609100930", "202609090931", "202609091001",
    "202602301000", "２０２６０９０９１０００", None, True])
def test_missing_stale_crossday_future_or_invalid_source_clock_rejected(bad):
    body = payload()
    body["data"]["todayFundTrend"]["minList"][0]["time"] = bad
    with pytest.raises(ValueError):
        parse(body)


@pytest.mark.parametrize("section", ["todayFundFlow", "todayFundTrend"])
def test_identity_binding_required(section):
    body = payload()
    body["data"][section]["stockCode"] = "sh600000"
    with pytest.raises(ValueError, match="identity"):
        parse(body)


@pytest.mark.parametrize("bad_code", [True, "0", 1, None, 0.0])
def test_response_status_is_not_coerced(bad_code):
    body = payload(); body["code"] = bad_code
    with pytest.raises(ValueError, match="business"):
        parse(body)


def test_summary_mismatch_does_not_borrow_previous_minute_clock():
    body = payload()
    body["data"]["todayFundTrend"]["minList"][0]["MainNetInflow"] = "99"
    with pytest.raises(ValueError, match="summary_trend"):
        parse(body)


@pytest.mark.parametrize("mode", ["duplicate", "unsorted", "empty", "excessive"])
def test_bad_minute_sequence_is_not_sorted_or_deduplicated_into_evidence(mode):
    body = payload(); points = body["data"]["todayFundTrend"]["minList"]
    if mode == "duplicate":
        points.append(deepcopy(points[0]))
    elif mode == "unsorted":
        points.append({**points[0], "time": "202609090959"})
    elif mode == "empty":
        points.clear()
    else:
        points *= 243
    with pytest.raises(ValueError):
        parse(body)


@pytest.mark.parametrize("error,accepted", [(1, True), (2, True), (3, False), (10000, False)])
def test_absolute_yuan_rounding_bound_is_not_a_percentage_tolerance(error, accepted):
    body = payload()
    body["data"]["todayFundFlow"]["retailIn"] = str(400 + error)
    if accepted:
        assert parse(body)
    else:
        with pytest.raises(ValueError, match="accounting"):
            parse(body)


def test_definition_change_is_not_silently_relabelled_as_same_main_funds():
    body = payload()
    body["data"]["todayFundFlow"]["desc"] = "总资金净流入"
    with pytest.raises(ValueError, match="definition"):
        parse(body)


def test_source_names_and_board_identity():
    assert tencent_fund_symbol("600519") == "sh600519"
    assert tencent_fund_symbol("300750") == "sz300750"
    assert tencent_fund_symbol("920001") == "bj920001"
    for code in ("０００００１", "000001.0", "sh600000", "1234567", "", "100000", True):
        with pytest.raises(ValueError):
            tencent_fund_symbol(code)


@pytest.fixture
def transport_env(monkeypatch):
    real_client = httpx.AsyncClient
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW
    monkeypatch.setattr(module, "datetime", Clock)
    monkeypatch.setattr(settings, "TENCENT_FUND_FLOW_CODES_PER_ROUND", 3)
    monkeypatch.setattr(settings, "TENCENT_FUND_FLOW_CONCURRENCY", 2)
    monkeypatch.setattr(settings, "TENCENT_FUND_FLOW_ROUND_TIMEOUT_SEC", 2.0)
    calls, client_options = [], []
    def install(handler=None):
        async def handle(request):
            calls.append(str(request.url))
            if handler:
                return await handler(request)
            return httpx.Response(200, json=payload(request.url.params["code"]))
        def factory(**kwargs):
            client_options.append(kwargs)
            return real_client(**kwargs, transport=httpx.MockTransport(handle))
        monkeypatch.setattr(module.httpx, "AsyncClient", factory)
        return calls, client_options
    return install


@pytest.mark.asyncio
async def test_rotation_unique_codes_same_pool_no_legacy_fetch(transport_env):
    calls, options = transport_env()
    source = TencentSource()
    first = await source.get_individual_fund_flow(["000004", "000001", "000002", "000003", "000001"])
    second = await source.get_individual_fund_flow(["000004", "000001", "000002", "000003"])
    assert list(first["代码"]) == ["000001", "000002", "000003"]
    assert list(second["代码"]) == ["000004", "000001", "000002"]
    assert len(calls) == 6 and all("hsfundtab" in url for url in calls)
    assert all("todayFundFlow%2CtodayFundTrend" in url for url in calls)
    assert options[0]["trust_env"] is False
    assert options[0]["limits"].max_connections == 2
    assert first.attrs["fund_flow_source_version"] == SOURCE_VERSION
    assert first.attrs["fund_flow_expected_count"] == 3
    assert first.attrs["fund_flow_universe_count"] == 4


@pytest.mark.asyncio
async def test_failed_identity_not_coverage_and_no_retry(transport_env):
    async def wrong(request):
        return httpx.Response(200, json=payload("sh600000"))
    calls, _ = transport_env(wrong)
    source = TencentSource()
    frame = await source.get_individual_fund_flow(["000001", "000002"])
    assert frame.empty and len(calls) == 2
    assert frame.attrs["fund_flow_expected_count"] == 2
    assert frame.attrs["fund_flow_errors"]["fund_identity_mismatch"] == 2
    assert (await source.get_individual_fund_flow(["000001"])).empty
    assert len(calls) == 2  # failed-round cooldown


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [403, 429])
async def test_denial_stops_round_and_respects_retry_after(transport_env, status):
    async def denied(request):
        return httpx.Response(status, headers={"Retry-After": "120"})
    calls, _ = transport_env(denied)
    source = TencentSource()
    start = module._time.monotonic()
    frame = await source.get_individual_fund_flow(["000001", "000002", "000003"])
    assert frame.empty and len(calls) <= 2
    assert source._fund_retry_at >= start + 120
    assert (await source.get_individual_fund_flow(["000001"])).attrs["fund_flow_errors"] == {"backoff": 1}


@pytest.mark.asyncio
async def test_timeout_cancels_workers_retains_completed_rows_and_no_reentry(transport_env, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    active = 0
    async def blocked(request):
        nonlocal active
        active += 1
        try:
            if request.url.params["code"] == "sz000001":
                return httpx.Response(200, json=payload("sz000001"))
            entered.set()
            await release.wait()
            return httpx.Response(200, json=payload(request.url.params["code"]))
        finally:
            active -= 1
    calls, _ = transport_env(blocked)
    monkeypatch.setattr(settings, "TENCENT_FUND_FLOW_ROUND_TIMEOUT_SEC", .15)
    source = TencentSource()
    task = asyncio.create_task(source.get_individual_fund_flow(["000001", "000002", "000003"]))
    await entered.wait()
    assert (await source.get_individual_fund_flow(["000004"])).attrs["fund_flow_errors"] == {"busy": 1}
    result = await task
    assert list(result["代码"]) == ["000001"]
    assert result.attrs["fund_flow_errors"]["round_timeout"] == 1
    assert active == 0 and not source._fund_busy
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_transport_failure_is_redacted_and_bounded(transport_env):
    async def failure(request):
        raise httpx.ConnectError("sensitive?token=SECRET", request=request)
    calls, _ = transport_env(failure)
    source = TencentSource()
    result = await source.get_individual_fund_flow(["000001"])
    assert result.empty and len(calls) == 1
    assert result.attrs["fund_flow_errors"] == {"ConnectError": 1}
    assert "SECRET" not in str(result.attrs)


def test_unified_fund_push_is_not_mislabelled_as_ths_total_flow():
    from app.push.templates.anomaly import _fund_source_label, _fund_source_method
    assert "统一资金" in _fund_source_label("fund_flow")
    assert "同花顺" not in _fund_source_label("fund_flow")
    assert "以来源证据为准" in _fund_source_method("fund_flow")
    assert "东方财富" in _fund_source_label("eastmoney_main_fund")


def test_settings_match_quote_six_connection_ceiling():
    from app.config.settings import Settings
    s = Settings(_env_file=None)
    assert s.TENCENT_FUND_FLOW_CONCURRENCY == 6
    assert s.TENCENT_FUND_FLOW_CODES_PER_ROUND == 600
    assert s.TENCENT_FUND_FLOW_ROUND_TIMEOUT_SEC < 30
    with pytest.raises(ValueError):
        Settings(_env_file=None, TENCENT_FUND_FLOW_CONCURRENCY=100)
    for bad in (float("nan"), float("inf"), -1):
        with pytest.raises(ValueError):
            Settings(_env_file=None, TENCENT_FUND_FLOW_REQUEST_INTERVAL_SEC=bad)
