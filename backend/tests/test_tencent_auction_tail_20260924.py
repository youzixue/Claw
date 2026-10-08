"""Real auction app + temporary SQL: per-response clocks, never vendor traffic."""
import asyncio
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import select

from app.data.auction_evidence import auction_evidence_status
from app.data.sources import tencent_source
from app.models.stock import AuctionData
from app.strategy import auction
from test_auction_source_frames_20260922 import maker

DAY = date(2026, 9, 23)


def at(second):
    return datetime(2026, 9, 23, 9, 25, second)


def quote(code="600000", **changes):
    return dict(dict(code=code, open=10.5, prev_close=10, volume=200,
                     amount=210000, volume_ratio=3, source_quote_at=at(5),
                     received_at=at(10), observed_at=at(11)), **changes)


def clock(monkeypatch, module, current):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return current[0]
    monkeypatch.setattr(module, "datetime", Clock)


def collection(monkeypatch, records, finish=at(32)):
    current = [at(1)]
    clock(monkeypatch, auction, current)

    async def collect(self, codes):
        current[0] = finish
        return records
    monkeypatch.setattr(tencent_source.TencentSource, "collect_spot_batch", collect)


@pytest.mark.asyncio
async def test_early_response_survives_late_tail_with_original_three_clocks(maker, monkeypatch):
    collection(monkeypatch, [
        quote(), quote("600001", source_quote_at=at(29), received_at=at(31), observed_at=at(32)),
    ])
    async with maker() as db:
        result = await auction.AuctionCollector().collect_tencent_auction_evidence(
            db, DAY, codes=["600000", "600001"])
        rows = list((await db.scalars(select(AuctionData))).all())
        assert [r.code for r in rows] == ["600000"]
        row = rows[0]
        assert (row.source_quote_at, row.received_at, row.observed_at) == (at(5), at(10), at(11))
        assert row.auction_time == "09:25:11"
        assert result["written"] == 1 and result["rejected"] == {"invalid_clock": 1}
        assert auction_evidence_status(row, decision_at=at(32)) == "ok"


@pytest.mark.asyncio
@pytest.mark.parametrize("changes,reason", [
    ({"received_at": None}, "unknown"),
    ({"observed_at": None}, "unknown"),
    ({"source_quote_at": None}, "unknown"),
    ({"observed_at": "2026-09-23 09:25:11"}, "unknown"),
    ({"source_quote_at": at(33)}, "future"),
    ({"received_at": at(4)}, "invalid_clock"),
    ({"source_quote_at": at(5)-timedelta(days=1)}, "invalid_clock"),
    ({"observed_at": at(11)-timedelta(days=1)}, "invalid_clock"),
    ({"source_quote_at": at(0)-timedelta(seconds=1)}, "unverified_basis"),
    ({"source_quote_at": at(0)-timedelta(seconds=2), "observed_at": at(30)}, "stale_source"),
    ({"volume_ratio": 0}, "incomplete_values"),
    ({"amount": 0}, "incomplete_values"),
    ({"observed_at": at(31), "received_at": at(30)}, "invalid_clock"),
])
async def test_reject_bad_row_without_losing_other_valid_response(maker, monkeypatch, changes, reason):
    collection(monkeypatch, [quote(), quote("600001", **changes)])
    async with maker() as db:
        result = await auction.AuctionCollector().collect_tencent_auction_evidence(db, DAY, codes=["600000", "600001"])
        assert [r.code for r in (await db.scalars(select(AuctionData))).all()] == ["600000"]
        assert result["rejected"] == {reason: 1}


@pytest.mark.asyncio
async def test_end_of_window_is_inclusive_but_microsecond_late_is_not(maker, monkeypatch):
    collection(monkeypatch, [
        quote(observed_at=at(30), received_at=at(30)),
        quote("600001", observed_at=at(30)+timedelta(microseconds=1), received_at=at(30)),
    ])
    async with maker() as db:
        result = await auction.AuctionCollector().collect_tencent_auction_evidence(db, DAY, codes=["600000", "600001"])
        assert result["written"] == 1 and result["rejected"] == {"invalid_clock": 1}


@pytest.mark.asyncio
async def test_all_late_rows_still_rejected(maker, monkeypatch):
    collection(monkeypatch, [quote(observed_at=at(31), received_at=at(31))])
    async with maker() as db:
        result = await auction.AuctionCollector().collect_tencent_auction_evidence(db, DAY, codes=["600000"])
        assert result["written"] == 0
        assert list((await db.scalars(select(AuctionData))).all()) == []


@pytest.mark.asyncio
async def test_duplicate_source_frame_preserves_first_payload_and_clocks(maker, monkeypatch):
    async with maker() as db:
        collection(monkeypatch, [quote()])
        await auction.AuctionCollector().collect_tencent_auction_evidence(db, DAY, codes=["600000"])
        collection(monkeypatch, [quote(open=11, observed_at=at(21), received_at=at(20))])
        await auction.AuctionCollector().collect_tencent_auction_evidence(db, DAY, codes=["600000"])
        rows = list((await db.scalars(select(AuctionData))).all())
        assert len(rows) == 1
        assert rows[0].auction_price == 10.5 and rows[0].observed_at == at(11)
        health = await auction.AuctionCollector().get_snapshot_health(db, DAY, as_of_at=at(32))
        assert health["multi_frame_complete_count"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError("collection failed"), asyncio.CancelledError()])
async def test_collection_exception_and_cancel_propagate_without_saving(maker, monkeypatch, failure):
    async def collect(self, codes):
        raise failure
    monkeypatch.setattr(tencent_source.TencentSource, "collect_spot_batch", collect)
    async with maker() as db:
        with pytest.raises(type(failure)):
            await auction.AuctionCollector().collect_tencent_auction_evidence(db, DAY, now=at(1), codes=["600000"])
        assert list((await db.scalars(select(AuctionData))).all()) == []


@pytest.mark.asyncio
async def test_legacy_record_without_observation_is_not_backdated_from_receipt(maker, monkeypatch):
    record = quote()
    del record["observed_at"]
    collection(monkeypatch, [record])
    async with maker() as db:
        result = await auction.AuctionCollector().collect_tencent_auction_evidence(db, DAY, codes=["600000"])
        assert result["written"] == 0


def raw_fields():
    fields = ["0"] * 88
    for i, value in {1: "测试股", 3: "10.5", 4: "10", 5: "10.5", 6: "200",
                     30: "20260923092505", 49: "3", 51: "10.5"}.items():
        fields[i] = value
    return fields


@pytest.mark.asyncio
async def test_actual_batch_source_stamps_each_response_before_late_gather(maker, monkeypatch):
    current = [at(1)]
    clock(monkeypatch, auction, current)
    clock(monkeypatch, tencent_source, current)

    class Client:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *exc): return False
    monkeypatch.setattr(tencent_source.httpx, "AsyncClient", Client)
    monkeypatch.setattr(tencent_source.TencentSource, "rate_limit", 0, raising=False)

    async def fetch(self, codes, client=None):
        if codes[0] == "600100":
            await asyncio.sleep(0)
            current[0] = at(32)
        else:
            current[0] = at(10)
        return {codes[0]: raw_fields()}
    monkeypatch.setattr(tencent_source.TencentSource, "_fetch_batch", fetch)
    codes = [f"600{i:03d}" for i in range(101)]
    async with maker() as db:
        result = await auction.AuctionCollector().collect_tencent_auction_evidence(db, DAY, codes=codes)
        rows = list((await db.scalars(select(AuctionData))).all())
        assert [r.code for r in rows] == ["600000"]
        assert rows[0].received_at == rows[0].observed_at == at(10)
        assert rows[0].volume_unit == "lot100" and rows[0].amount_unit == "CNY"
        assert result["rejected"] == {"invalid_clock": 1}


@pytest.mark.asyncio
@pytest.mark.parametrize("capture", [False, True])
async def test_ordinary_source_payload_unchanged_unless_clock_capture_opted_in(monkeypatch, capture):
    current = [at(10)]
    clock(monkeypatch, tencent_source, current)

    class Client:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *exc): return False
    monkeypatch.setattr(tencent_source.httpx, "AsyncClient", Client)

    async def fetch(self, codes, client=None):
        return {codes[0]: raw_fields()}
    monkeypatch.setattr(tencent_source.TencentSource, "_fetch_batch", fetch)
    source = tencent_source.TencentSource(capture_response_observed_at=capture)
    expected = source._parse_spot("600000", raw_fields(), received_at=at(10))
    # Use a fresh instance so existing orderbook-difference state is unchanged.
    records = await tencent_source.TencentSource(capture_response_observed_at=capture).collect_spot_batch(["600000"])
    assert len(records) == 1
    if capture:
        assert records[0].pop("observed_at") == at(10)
    else:
        assert "observed_at" not in records[0]
    assert records[0] == expected


@pytest.mark.asyncio
async def test_late_sync_rescue_does_not_erase_async_early_response(maker, monkeypatch):
    current = [at(1)]
    clock(monkeypatch, auction, current)
    clock(monkeypatch, tencent_source, current)

    class Client:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *exc): return False
    monkeypatch.setattr(tencent_source.httpx, "AsyncClient", Client)
    monkeypatch.setattr(tencent_source.TencentSource, "rate_limit", 0)

    async def fetch(self, codes, client=None):
        current[0] = at(10)
        return {"600000": raw_fields()} if "600000" in codes else {}

    def rescue(self, codes):
        current[0] = at(32)
        return {"600499": raw_fields()}
    monkeypatch.setattr(tencent_source.TencentSource, "_fetch_batch", fetch)
    monkeypatch.setattr(tencent_source.TencentSource, "_fetch_missing_batches_sync", rescue)
    async with maker() as db:
        result = await auction.AuctionCollector().collect_tencent_auction_evidence(
            db, DAY, codes=[f"600{i:03d}" for i in range(500)])
        rows = list((await db.scalars(select(AuctionData))).all())
        assert [r.code for r in rows] == ["600000"]
        assert rows[0].received_at == rows[0].observed_at == at(10)
        assert result["rejected"] == {"invalid_clock": 1}


@pytest.mark.asyncio
async def test_actual_source_cancellation_after_early_response_propagates(maker, monkeypatch):
    current = [at(1)]
    clock(monkeypatch, auction, current)
    clock(monkeypatch, tencent_source, current)
    late_started = asyncio.Event()
    late_cancelled = asyncio.Event()

    class Client:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *exc): return False
    monkeypatch.setattr(tencent_source.httpx, "AsyncClient", Client)
    monkeypatch.setattr(tencent_source.TencentSource, "rate_limit", 0)

    async def fetch(self, codes, client=None):
        if codes[0] == "600100":
            late_started.set()
            try:
                await asyncio.Future()
            finally:
                late_cancelled.set()
        current[0] = at(10)
        return {codes[0]: raw_fields()}
    monkeypatch.setattr(tencent_source.TencentSource, "_fetch_batch", fetch)
    async with maker() as db:
        task = asyncio.create_task(auction.AuctionCollector().collect_tencent_auction_evidence(
            db, DAY, codes=[f"600{i:03d}" for i in range(101)]))
        await asyncio.wait_for(late_started.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert late_cancelled.is_set()
        assert list((await db.scalars(select(AuctionData))).all()) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError("save failed"), asyncio.CancelledError()])
async def test_save_exception_and_cancel_are_not_converted_to_success(maker, monkeypatch, failure):
    collection(monkeypatch, [quote()])
    async def fail_save(*args, **kwargs):
        raise failure
    monkeypatch.setattr(auction.AuctionCollector, "_save_verified_auction_candidates", fail_save)
    async with maker() as db:
        with pytest.raises(type(failure)):
            await auction.AuctionCollector().collect_tencent_auction_evidence(db, DAY, codes=["600000"])
        assert list((await db.scalars(select(AuctionData))).all()) == []

