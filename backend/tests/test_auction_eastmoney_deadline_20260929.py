"""Offline Eastmoney deadline/rescue boundaries; no live HTTP or production writes."""
import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from app.strategy import auction as module
from test_auction_eastmoney_rate_limit_20260918 import _Resp, _ok_batch, _row, _secids, patch_client

START = datetime(2026, 9, 29, 9, 25, 5)
END = datetime(2026, 9, 29, 9, 25, 30)


def clock_at(monkeypatch, at=START):
    current = [at]
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return current[0]
    monkeypatch.setattr(module, "datetime", Clock)
    monkeypatch.setattr(module, "EASTMONEY_AUCTION_PACE_SEC", 0)
    monkeypatch.setattr(module, "EASTMONEY_AUCTION_BACKOFF_SEC", 0)
    return current


@pytest.mark.asyncio
async def test_partial_batch_rescue_only_requests_missing_and_never_overwrites_first(patch_client, monkeypatch):
    at = clock_at(monkeypatch)
    def response(url):
        if module.EASTMONEY_QUOTE_HOSTS[0] in url:
            return _Resp({"data": {"diff": [_row("600001")]}})
        at[0] = END + timedelta(seconds=1)
        # Even if a vendor sends unsolicited old code, keep the earlier received row.
        return _Resp({"data": {"diff": [_row("600001"), _row("600002")]}})
    get = patch_client([response])
    quotes = await module.AuctionCollector()._fetch_eastmoney_quotes(["600001", "600002"])
    assert _secids(get.calls[1]) == ["1.600002"]
    assert quotes["600001"]["received_at"] == START
    assert quotes["600002"]["received_at"] > END


@pytest.mark.asyncio
async def test_previous_host_throttle_does_not_poison_fallback_network_error(patch_client, monkeypatch):
    clock_at(monkeypatch)
    monkeypatch.setattr(module, "EASTMONEY_AUCTION_BATCH", 1)
    def response(url):
        if module.EASTMONEY_QUOTE_HOSTS[0] in url:
            return _Resp({}, status_code=429)
        if "600001" in url:
            raise ConnectionError("isolated network failure")
        return _ok_batch(url)
    patch_client([response])
    diag = {}
    quotes = await module.AuctionCollector()._fetch_eastmoney_quotes(["600001", "600002"], diagnostics=diag)
    assert set(quotes) == {"600002"}
    assert diag["rate_limited"] is True
    assert diag["throttled_batches"] == 1
    assert diag["failed_batches"] == 2


@pytest.mark.asyncio
async def test_real_json_decode_error_is_classified_as_throttling(patch_client, monkeypatch):
    clock_at(monkeypatch)
    class HtmlResponse(_Resp):
        def json(self):
            raise ValueError("not JSON")
    def response(url):
        return HtmlResponse(None) if module.EASTMONEY_QUOTE_HOSTS[0] in url else _ok_batch(url)
    patch_client([response])
    diag = {}
    quotes = await module.AuctionCollector()._fetch_eastmoney_quotes(["600001"], diagnostics=diag)
    assert set(quotes) == {"600001"}
    assert diag["rate_limited"] is True and diag["throttled_batches"] == 1


@pytest.mark.asyncio
async def test_deadline_stops_new_batches_and_retains_earlier_response(patch_client, monkeypatch):
    at = clock_at(monkeypatch)
    monkeypatch.setattr(module, "EASTMONEY_AUCTION_BATCH", 1)
    def response(url):
        if "600002" in url:
            at[0] = END + timedelta(seconds=1)
            raise ConnectionError("tail took too long")
        return _ok_batch(url)
    get = patch_client([response])
    diag = {}
    quotes = await module.AuctionCollector()._fetch_eastmoney_quotes(
        ["600001", "600002", "600003"], diagnostics=diag, deadline_at=END)
    assert set(quotes) == {"600001"}
    assert len(get.calls) == 2, "no retry/fallback/third batch after real cutoff"
    assert diag["deadline_exhausted"] is True and diag["missing_codes"] == 2


@pytest.mark.asyncio
async def test_expired_deadline_sends_no_requests(patch_client, monkeypatch):
    clock_at(monkeypatch, END+timedelta(microseconds=1))
    get = patch_client([_ok_batch])
    diag = {}
    assert await module.AuctionCollector()._fetch_eastmoney_quotes(
        ["600001"], diagnostics=diag, deadline_at=END) == {}
    assert get.calls == []
    assert diag["deadline_exhausted"] and diag["missing_codes"] == 1


@pytest.mark.asyncio
async def test_inflight_deadline_cancels_tail_but_keeps_success(monkeypatch):
    clock_at(monkeypatch)
    monkeypatch.setattr(module, "EASTMONEY_AUCTION_BATCH", 1)
    cancelled, calls = [], []
    class Client:
        def __init__(self, *args, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False
        async def get(self, url, params=None, **kwargs):
            code = params["secids"].split(".")[1]
            calls.append(code)
            if code == "600001":
                return _Resp({"data": {"diff": [_row(code)]}})
            try:
                await asyncio.sleep(60)
            finally:
                cancelled.append(code)
    monkeypatch.setattr("httpx.AsyncClient", Client)
    diag = {}
    # Monotonic budget must progress even while the wall clock is frozen in this test.
    quotes = await asyncio.wait_for(module.AuctionCollector()._fetch_eastmoney_quotes(
        ["600001", "600002", "600003"], diagnostics=diag,
        deadline_at=START+timedelta(seconds=0.1)), timeout=2)
    assert set(quotes) == {"600001"}
    assert calls == ["600001", "600002"] and cancelled == ["600002"]
    assert diag["deadline_exhausted"] is True


@pytest.mark.asyncio
async def test_caller_cancellation_propagates_without_retry(monkeypatch):
    clock_at(monkeypatch)
    entered, closed = asyncio.Event(), []
    class Client:
        def __init__(self, *args, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): closed.append(True); return False
        async def get(self, *args, **kwargs):
            entered.set()
            await asyncio.sleep(60)
    monkeypatch.setattr("httpx.AsyncClient", Client)
    task = asyncio.create_task(module.AuctionCollector()._fetch_eastmoney_quotes(
        ["600001"], deadline_at=END))
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert closed == [True]
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_backoff_that_cannot_fit_stops_without_retry(patch_client, monkeypatch):
    clock_at(monkeypatch, END-timedelta(seconds=0.05))
    monkeypatch.setattr(module, "EASTMONEY_AUCTION_BACKOFF_SEC", 0.35)
    get = patch_client([lambda url: _Resp({}, status_code=429)])
    diag = {}
    assert await module.AuctionCollector()._fetch_eastmoney_quotes(
        ["600001"], diagnostics=diag, deadline_at=END) == {}
    assert len(get.calls) == 1 and diag["deadline_exhausted"] is True
    assert diag["missing_codes"] == 1


@pytest.mark.asyncio
async def test_final_successful_http_after_cutoff_does_not_clear_deadline_diagnostic(patch_client, monkeypatch):
    at = clock_at(monkeypatch)
    def response(url):
        at[0] = END+timedelta(seconds=1)
        return _ok_batch(url)
    get = patch_client([response])
    diag = {}
    quotes = await module.AuctionCollector()._fetch_eastmoney_quotes(
        ["600001"], diagnostics=diag, deadline_at=END)
    assert len(get.calls) == 1
    assert quotes["600001"]["received_at"] > END  # unchanged gate rejects this, never backdate
    assert diag["deadline_exhausted"] is True


@pytest.mark.asyncio
async def test_collector_always_supplies_original_terminal_cutoff(monkeypatch):
    clock_at(monkeypatch)
    collector = module.AuctionCollector()
    fetch = AsyncMock(return_value={})
    monkeypatch.setattr(collector, "_fetch_eastmoney_quotes", fetch)
    await collector.collect_eastmoney_auction_evidence(AsyncMock(), START.date(), codes=["600001"])
    assert fetch.await_args.kwargs["deadline_at"] == END
    assert fetch.await_args.kwargs["now"] is None
