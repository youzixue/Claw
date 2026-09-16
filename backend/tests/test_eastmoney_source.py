"""Isolated fund-source contract tests; no real endpoints or business DB writes."""
import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from unittest.mock import AsyncMock

import httpx
import pytest

from app.data.sources.eastmoney_source import EastMoneySource


def row(code="000001"):
    # Deliberately unlike the provider's usual JSON order.
    return {"f124": int(datetime.now().timestamp()),
            "f184": 5.2, "f62": 123456, "f12": code, "f14": "测试",
            "f2": 10, "f3": 1, "f66": 20, "f69": 1,
            "f72": 30, "f75": 2, "f78": -10, "f81": -1,
            "f84": -40, "f87": -2, "unexpected": "ignored"}


def payload(rows, total=None):
    return {"data": {"total": len(rows) if total is None else total, "diff": rows}}


@pytest.fixture
def install_client(monkeypatch):
    real_client = httpx.AsyncClient
    calls = []

    def install(handler):
        def factory(**kwargs):
            assert kwargs["trust_env"] is False
            async def dispatch(request):
                assert request.url.params["fid"] == "f12"  # Stable pagination, not a moving flow rank.
                calls.append(int(request.url.params["pn"]))
                return handler(request, len(calls))
            return real_client(**kwargs, transport=httpx.MockTransport(dispatch))
        monkeypatch.setattr("app.data.sources.eastmoney_source.httpx.AsyncClient", factory)
        return calls

    monkeypatch.setattr("app.data.sources.eastmoney_source.asyncio.sleep", AsyncMock())
    return install


@pytest.mark.asyncio
async def test_key_mapping_and_first_page_reused(install_client):
    rows = [row(f"{n:06}") for n in range(101)]
    def handler(request, _):
        page = int(request.url.params["pn"])
        return httpx.Response(200, json=payload(rows[:100] if page == 1 else rows[100:], 101))
    calls = install_client(handler)
    result = await EastMoneySource().get_individual_fund_flow()
    assert calls == [1, 2]
    assert len(result) == 101
    assert result.iloc[0]["代码"] == "000000"
    assert result.iloc[0]["今日主力净流入-净额"] == 123456
    assert result.iloc[0]["今日主力净流入-净占比"] == 5.2
    assert result.iloc[0]["今日大单净流入-净额"] == 30


@pytest.mark.asyncio
async def test_stable_code_pagination_restores_public_flow_ranking(install_client):
    low, high = row("000001"), row("000002")
    low["f62"], high["f62"] = -10, 20
    install_client(lambda *_: httpx.Response(200, json=payload([low, high])))
    result = await EastMoneySource().get_individual_fund_flow()
    assert result["代码"].tolist() == ["000002", "000001"]
    assert result["序号"].tolist() == [1, 2]
    assert result["今日主力净流入-净额"].tolist() == [20, -10]


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [None, {}, {"data": None}, payload([]),
                                  payload([row()], 101), {"data": {"total": 1, "diff": {}}},
                                  {"data": {"total": True, "diff": [row()]}}])
async def test_empty_or_malformed_response_fails_explicitly(install_client, body):
    calls = install_client(lambda *_: httpx.Response(200, json=body))
    with pytest.raises(ValueError):
        await EastMoneySource().get_individual_fund_flow()
    assert calls == [1]


@pytest.mark.asyncio
@pytest.mark.parametrize("second", [payload([], 101), payload([row("000000")], 101),
                                   payload([row("000100")], 102)])
async def test_incomplete_duplicate_or_changing_pages_are_not_published(install_client, second):
    first = payload([row(f"{n:06}") for n in range(100)], 101)
    install_client(lambda _, n: httpx.Response(200, json=first if n == 1 else second))
    with pytest.raises(ValueError):
        await EastMoneySource().get_individual_fund_flow()


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["f62", "f184", "f66", "f69", "f72", "f75", "f78", "f81", "f84", "f87"])
@pytest.mark.parametrize("value", [None, "-", "nan", float("inf"), True, False])
async def test_missing_numeric_is_not_fabricated_as_zero(install_client, value, field):
    item = row()
    item[field] = str(value) if isinstance(value, float) else value
    install_client(lambda *_: httpx.Response(200, json=payload([item])))
    with pytest.raises(ValueError, match="numeric"):
        await EastMoneySource().get_individual_fund_flow()


@pytest.mark.asyncio
async def test_genuine_zero_is_preserved_and_next_empty_round_is_not_cached(install_client):
    item = row()
    item["f62"] = 0
    install_client(lambda _, n: httpx.Response(200, json=payload([item]) if n == 1 else payload([])))
    source = EastMoneySource()
    result = await source.get_individual_fund_flow()
    assert result.iloc[0]["今日主力净流入-净额"] == 0
    with pytest.raises(ValueError):
        await source.get_individual_fund_flow()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["disconnect", "timeout", "429", "503"])
async def test_transient_failure_is_retried_then_succeeds(install_client, kind):
    def handler(request, n):
        if n == 1:
            if kind == "disconnect":
                raise httpx.RemoteProtocolError("disconnected", request=request)
            if kind == "timeout":
                raise httpx.ReadTimeout("timeout", request=request)
            return httpx.Response(int(kind))
        return httpx.Response(200, json=payload([row()]))
    calls = install_client(handler)
    assert len(await EastMoneySource().get_individual_fund_flow()) == 1
    assert calls == [1, 1]
    from app.data.sources.eastmoney_source import asyncio as source_asyncio
    assert any(call.args == (1,) for call in source_asyncio.sleep.call_args_list)


@pytest.mark.asyncio
async def test_retry_exhaustion_has_bounded_requests_and_redacted_error(install_client):
    def handler(request, _):
        raise httpx.ProxyError("sensitive-proxy-credential", request=request)
    calls = install_client(handler)
    with pytest.raises(RuntimeError, match="ProxyError") as error:
        await EastMoneySource().get_individual_fund_flow()
    assert "sensitive" not in str(error.value)
    assert len(calls) == EastMoneySource.max_retries


@pytest.mark.asyncio
async def test_non_retryable_status_fails_once(install_client):
    calls = install_client(lambda *_: httpx.Response(403))
    with pytest.raises(RuntimeError):
        await EastMoneySource().get_individual_fund_flow()
    assert calls == [1]


@pytest.mark.asyncio
async def test_cancellation_is_not_retried(install_client):
    def handler(*_):
        raise asyncio.CancelledError()
    calls = install_client(handler)
    with pytest.raises(asyncio.CancelledError):
        await EastMoneySource().get_individual_fund_flow()
    assert calls == [1]


@pytest.mark.asyncio
async def test_missing_required_field_is_rejected(install_client):
    item = row()
    del item["f72"]
    install_client(lambda *_: httpx.Response(200, json=payload([item])))
    with pytest.raises(ValueError, match="required fields"):
        await EastMoneySource().get_individual_fund_flow()


@pytest.mark.asyncio
async def test_whole_round_deadline_cancels_pending_request(monkeypatch):
    real_client = httpx.AsyncClient
    real_timeout = asyncio.timeout

    async def never_respond(_):
        await asyncio.Event().wait()

    monkeypatch.setattr(
        "app.data.sources.eastmoney_source.httpx.AsyncClient",
        lambda **kwargs: real_client(**kwargs, transport=httpx.MockTransport(never_respond)),
    )

    def short_deadline(seconds):
        assert seconds == 20
        return real_timeout(0.01)

    monkeypatch.setattr("app.data.sources.eastmoney_source.asyncio.timeout", short_deadline)
    with pytest.raises(TimeoutError, match="collection exceeded"):
        await EastMoneySource().get_individual_fund_flow()


@pytest.mark.asyncio
async def test_unsupported_indicator_never_calls_network(install_client):
    calls = install_client(lambda *_: pytest.fail("network should not be called"))
    with pytest.raises(ValueError):
        await EastMoneySource().get_individual_fund_flow("5日")
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("clock", [None, "-", True, 0, 1, 9999999999999])
async def test_missing_stale_or_invalid_f124_cannot_be_receipt_stamped(install_client, clock):
    item = row()
    item["f124"] = clock
    install_client(lambda *_: httpx.Response(200, json=payload([item])))
    with pytest.raises(ValueError, match="source quote clocks"):
        await EastMoneySource().get_individual_fund_flow()


@pytest.mark.asyncio
async def test_clock_filter_preserves_original_coverage_denominator(install_client):
    fresh, stale = row("000001"), row("000002")
    stale["f124"] = 1
    install_client(lambda *_: httpx.Response(200, json=payload([fresh, stale])))
    result = await EastMoneySource().get_individual_fund_flow()
    assert result["代码"].tolist() == ["000001"]
    assert result.attrs["fund_flow_expected_count"] == 2
    assert result.attrs["fund_flow_clock_rejected_count"] == 1
    source_at = datetime.fromtimestamp(fresh["f124"], ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)
    assert result.iloc[0]["source_quote_at"] == source_at
    assert source_at <= result.iloc[0]["received_at"] <= result.attrs["fund_flow_observed_at"]


@pytest.mark.asyncio
async def test_receipt_clock_is_per_page_not_end_of_pagination(install_client, monkeypatch):
    now = datetime.now().replace(microsecond=0)
    current = [now]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return current[0]

    monkeypatch.setattr("app.data.sources.eastmoney_source.datetime", Clock)
    rows = [row(f"{n:06}") for n in range(101)]
    for item in rows:
        item["f124"] = int(now.timestamp())

    def handler(request, _):
        page = int(request.url.params["pn"])
        current[0] = now + timedelta(seconds=page)
        return httpx.Response(200, json=payload(rows[:100] if page == 1 else rows[100:], 101))

    install_client(handler)
    result = await EastMoneySource().get_individual_fund_flow()
    assert result.iloc[0]["received_at"] == now + timedelta(seconds=1)
    assert result.iloc[-1]["received_at"] == now + timedelta(seconds=2)
