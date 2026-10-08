"""Explicit dated resumption, transactional audit and conservative risk boundaries."""
import asyncio
import json
from datetime import date, datetime, timedelta
from unittest.mock import AsyncMock

import pandas as pd
import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.data.scheduler as module
from app.core.stock_tagger import stock_tagger
from app.data.sources.wencai_stream_source import parse_dated_trading_status
from app.db.session import Base
from app.models.stock import StockBlacklist, StockTag
from app.models.governance import DataWatermark, DataWatermarkRevision

DAY = date(2026, 9, 29)
NOW = datetime(2026, 9, 29, 16, 30)
CODE = "600825"
NAME = "新华传媒"


def frame(state="交易", code="600825.SH", name=NAME, day=DAY):
    data = pd.DataFrame([{"股票代码": code, "股票简称": name, f"交易状态[{day:%Y%m%d}]": state}])
    data.attrs.update(code_count=1, row_count=1)
    return data


@pytest.mark.parametrize("raw,expected", [
    ("交易", "trading"), (" 交易 ", "trading"),
    ("重要公告，停牌自2026-09-15起连续停牌", "suspended"),
    ("重大事项，停牌1天", "suspended"), ("重要公告，停牌全天", "suspended"),
    ("停牌", "suspended"), ("新股上市", "unknown"), ("复牌", "unknown"),
    ("未停牌", "unknown"), ("正常交易待核验", "unknown"), (None, "unknown"),
    ("停牌自2026-10-01起连续停牌", "unknown"),
    ("停牌自2026-02-31起连续停牌", "unknown"),
])
def test_strict_states(raw, expected):
    assert parse_dated_trading_status(frame(raw), DAY)[CODE]["state"] == expected


@pytest.mark.parametrize("bad", [
    None, pd.DataFrame(), frame(day=date(2026, 9, 28)),
    frame(code="600825.SZ"), frame(code=600825), frame(name="nan"),
    frame(code="999999"), pd.concat([frame(), frame()], ignore_index=True),
])
def test_bad_batch_cannot_certify(bad):
    with pytest.raises(ValueError):
        parse_dated_trading_status(bad, DAY)


@pytest.mark.parametrize("key,value", [("code_count", None), ("code_count", 2),
    ("code_count", True), ("row_count", 0), ("row_count", float("nan"))])
def test_truncated_or_unverified_coverage(key, value):
    data = frame()
    data.attrs[key] = value
    with pytest.raises(ValueError, match="coverage"):
        parse_dated_trading_status(data, DAY)


@pytest_asyncio.fixture
async def setup(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'status.db'}")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    monkeypatch.setattr(module, "async_session", factory)
    monkeypatch.setattr(module.trade_calendar, "is_trade_day", AsyncMock(return_value=True))
    failure = AsyncMock()
    monkeypatch.setattr(module.data_quality_guard, "record_failure", failure)
    class Clock:
        @staticmethod
        def now():
            return NOW
    monkeypatch.setattr(module, "datetime", Clock)
    async with factory() as db:
        db.add(StockTag(code=CODE, name=NAME, board_type="main_sh", board_tag="suspended",
                       is_suspended=True, is_st=False, is_delisting=False,
                       is_ipo_recent=False, is_limit_up=True, is_limit_down=False,
                       suspend_reason="原停牌原因", updated_at=NOW - timedelta(days=1)))
        db.add(StockBlacklist(code=CODE, reason="suspended", source="auto",
                              start_date=date(2026, 9, 8), auto_expire=False))
        await db.commit()
    yield factory, failure
    await engine.dispose()


def source(monkeypatch, data=None, hook=None, error=None):
    class Source:
        async def query_async(self, query, *, perpage):
            assert query == "全部A股 2026年09月29日交易状态"
            assert perpage == 10000
            if hook:
                await hook()
            if error:
                raise error
            return data if data is not None else frame()
    monkeypatch.setattr(module, "WencaiStreamSource", Source)


async def snapshot(factory):
    async with factory() as db:
        return stock_tagger.risk_fingerprint(await db.get(StockTag, CODE),
                                            await db.get(StockBlacklist, CODE))


@pytest.mark.asyncio
async def test_resumed_auto_halt_clears_atomically_with_immutable_original(setup, monkeypatch):
    factory, failure = setup
    before = await snapshot(factory)
    source(monkeypatch)
    result = await module.DataScheduler()._refresh_verified_trading_status()
    assert result["automatic_clear_count"] == 1 and result["cleared_codes"] == [CODE]
    after = await snapshot(factory)
    assert after["blacklist"] is None
    assert after["tag"]["is_suspended"] is False
    assert after["tag"]["board_tag"] == "tradeable"
    for key in before["tag"].keys() - {"is_suspended", "board_tag", "updated_at"}:
        assert before["tag"][key] == after["tag"][key]
    async with factory() as db:
        watermark = await db.scalar(select(DataWatermark))
        revisions = (await db.scalars(select(DataWatermarkRevision))).all()
        assert len(revisions) == 1
        details = json.loads(revisions[0].details_json)
        assert details["transitions"] == [{"code": CODE, "before": before, "after": after,
                                           "evidence": {"name": NAME, "state": "trading", "raw_state": "交易"}}]
        assert details["provider_timestamp"] is None and not details["execution_authorized"]
        assert watermark.observed_at == NOW
        with pytest.raises(Exception, match="append-only"):
            await db.execute(text("DELETE FROM data_watermark_revision"))
        await db.rollback()
    failure.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("fields", [
    {"source": "manual"}, {"reason": "manual"}, {"reason": "st"},
    {"reason": "delisting"}, {"auto_expire": True}, {"end_date": DAY},
    {"end_date": DAY - timedelta(days=1)}, {"start_date": DAY},
    {"start_date": DAY + timedelta(days=1)},
])
async def test_independent_or_same_day_restrictions_never_removed(setup, monkeypatch, fields):
    factory, _ = setup
    async with factory() as db:
        ban = await db.get(StockBlacklist, CODE)
        for key, value in fields.items():
            setattr(ban, key, value)
        await db.commit()
    before = await snapshot(factory)
    source(monkeypatch)
    result = await module.DataScheduler()._refresh_verified_trading_status()
    assert result["automatic_clear_count"] == 0
    assert await snapshot(factory) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("fields", [
    {"is_st": True}, {"is_delisting": True}, {"is_ipo_recent": True},
    {"board_tag": "blocked"}, {"board_tag": "observe_only"},
])
async def test_other_tag_risks_cannot_be_released(setup, monkeypatch, fields):
    factory, _ = setup
    async with factory() as db:
        tag = await db.get(StockTag, CODE)
        for key, value in fields.items():
            setattr(tag, key, value)
        await db.commit()
    before = await snapshot(factory)
    source(monkeypatch)
    result = await module.DataScheduler()._refresh_verified_trading_status()
    after = await snapshot(factory)
    if "board_tag" in fields:
        assert result["automatic_clear_count"] == 0 and before == after
    else:
        assert result["automatic_clear_count"] == 1
        assert after["tag"]["board_tag"] == "blocked"
        for key in fields:
            assert after["tag"][key] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("data", [frame("停牌"), frame("未知"),
    frame(code="600001.SH", name="其他股票"), frame(name="改名未核验"),
    frame(day=DAY-timedelta(days=1))])
async def test_missing_unknown_wrong_date_or_identity_preserves(setup, monkeypatch, data):
    factory, _ = setup
    before = await snapshot(factory)
    source(monkeypatch, data)
    result = await module.DataScheduler()._refresh_verified_trading_status()
    assert result["automatic_clear_count"] == 0
    assert await snapshot(factory) == before


@pytest.mark.asyncio
async def test_manual_change_during_network_is_not_overwritten(setup, monkeypatch):
    factory, _ = setup
    async def change():
        async with factory() as db:
            ban = await db.get(StockBlacklist, CODE)
            ban.source, ban.reason = "manual", "人工确认"
            await db.commit()
    source(monkeypatch, hook=change)
    result = await module.DataScheduler()._refresh_verified_trading_status()
    assert result["held"][CODE] == "risk_changed_during_fetch"
    after = await snapshot(factory)
    assert after["tag"]["is_suspended"]
    assert after["blacklist"]["source"] == "manual"


@pytest.mark.asyncio
async def test_audit_failure_rolls_back_tag_and_blacklist(setup, monkeypatch):
    factory, _ = setup
    before = await snapshot(factory)
    async with factory() as db:
        await db.execute(text("""CREATE TRIGGER fail_status_audit BEFORE INSERT ON data_watermark_revision
            BEGIN SELECT RAISE(ABORT, 'injected audit failure'); END"""))
        await db.commit()
    source(monkeypatch)
    result = await module.DataScheduler()._refresh_verified_trading_status()
    assert result["status"] == "failed" and "injected audit failure" in result["reason"]
    assert await snapshot(factory) == before
    async with factory() as db:
        assert (await db.scalars(select(DataWatermark))).all() == []


@pytest.mark.asyncio
async def test_timeout_preserves_and_cancellation_propagates(setup, monkeypatch):
    factory, _ = setup
    before = await snapshot(factory)
    source(monkeypatch, error=TimeoutError("source timeout"))
    assert (await module.DataScheduler()._refresh_verified_trading_status())["status"] == "failed"
    assert await snapshot(factory) == before
    source(monkeypatch, error=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await module.DataScheduler()._refresh_verified_trading_status()
    assert await snapshot(factory) == before


@pytest.mark.asyncio
async def test_newer_response_and_cross_day_are_rejected(setup, monkeypatch):
    factory, _ = setup
    before = await snapshot(factory)
    async with factory() as db:
        db.add(DataWatermark(dataset="stock_trading_status", trade_date=DAY,
                             observed_at=NOW, status="ok"))
        await db.commit()
    source(monkeypatch)
    result = await module.DataScheduler()._refresh_verified_trading_status()
    assert result["status"] == "failed" and "newer" in result["reason"]
    assert await snapshot(factory) == before
    ticks = iter([NOW, NOW + timedelta(days=1)])
    monkeypatch.setattr(module.datetime, "now", lambda: next(ticks))
    result = await module.DataScheduler()._refresh_verified_trading_status()
    assert result["status"] == "failed" and "crossed date" in result["reason"]
    assert await snapshot(factory) == before


@pytest.mark.asyncio
async def test_positive_conflict_and_missing_auto_provenance_hold(setup, monkeypatch):
    factory, _ = setup
    before = await snapshot(factory)
    source(monkeypatch)
    result = await module.DataScheduler()._refresh_verified_trading_status(blocked_codes={CODE})
    assert result["held"][CODE] == "positive_halt_evidence_in_same_refresh"
    assert await snapshot(factory) == before
    async with factory() as db:
        await db.delete(await db.get(StockBlacklist, CODE))
        await db.commit()
    # A newer observation, not a replay of the same timestamp.
    monkeypatch.setattr(module.datetime, "now", lambda: NOW + timedelta(minutes=1))
    result = await module.DataScheduler()._refresh_verified_trading_status()
    assert result["held"][CODE] == "automatic_halt_provenance_missing"
    assert (await snapshot(factory))["tag"]["is_suspended"]


@pytest.mark.asyncio
@pytest.mark.parametrize("fields", [{"start_date": None}, {"auto_expire": None}])
async def test_nullable_legacy_provenance_never_releases(fields):
    tag = StockTag(code=CODE, name=NAME, board_type="main_sh", board_tag="suspended",
                   is_suspended=True)
    ban = StockBlacklist(code=CODE, source="auto", reason="suspended",
                         start_date=DAY-timedelta(days=1), auto_expire=False)
    for key, value in fields.items():
        setattr(ban, key, value)
    db = AsyncMock()
    outcome = await stock_tagger.clear_verified_auto_suspension(db, tag, ban,
        evidence={"state": "trading", "name": NAME}, trade_date=DAY, observed_at=NOW)
    assert outcome == "independent_or_unverified_restriction"
    db.delete.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("code,board", [("300001", "gem"), ("688001", "star"), ("920001", "bse")])
async def test_resume_never_promotes_observation_boards(code, board):
    tag = StockTag(code=code, name=NAME, board_type=board, board_tag="suspended",
                   is_suspended=True)
    ban = StockBlacklist(code=code, source="auto", reason="suspended",
                         start_date=DAY-timedelta(days=1), auto_expire=False)
    outcome = await stock_tagger.clear_verified_auto_suspension(AsyncMock(), tag, ban,
        evidence={"state": "trading", "name": NAME}, trade_date=DAY, observed_at=NOW)
    assert outcome == "auto_suspension_cleared" and tag.board_tag == "observe_only"


@pytest.mark.asyncio
async def test_repeat_observation_archives_without_duplicate_release(setup, monkeypatch):
    factory, _ = setup
    source(monkeypatch)
    collector = module.DataScheduler()
    assert (await collector._refresh_verified_trading_status())["automatic_clear_count"] == 1
    after = await snapshot(factory)
    monkeypatch.setattr(module.datetime, "now", lambda: NOW + timedelta(minutes=1))
    assert (await collector._refresh_verified_trading_status())["automatic_clear_count"] == 0
    assert await snapshot(factory) == after
    async with factory() as db:
        revisions = (await db.scalars(select(DataWatermarkRevision))).all()
        assert len(revisions) == 3
        assert sum(len(json.loads(r.details_json)["transitions"]) for r in revisions) == 2


@pytest.mark.asyncio
async def test_end_to_end_refresh_and_new_halt_after_resume(setup, monkeypatch):
    factory, _ = setup
    current = ["交易"]
    class Source:
        async def query_async(self, query, *, perpage):
            if query.startswith("全部A股"):
                return frame(current[0])
            if query == "停牌" and current[0] == "停牌":
                return pd.DataFrame([{"股票代码": "600825.SH", "股票简称": NAME}])
            if query in ("ST股", "*ST股"):
                return pd.DataFrame([{"股票代码": "600001.SH", "股票简称": "*ST其他"}])
            return pd.DataFrame()
    monkeypatch.setattr(module, "WencaiStreamSource", Source)
    collector = module.DataScheduler()
    result = await collector._update_stock_status()
    assert result["automatic_clear_count"] == 1
    current[0] = "停牌"
    monkeypatch.setattr(module.datetime, "now", lambda: NOW + timedelta(minutes=1))
    result = await collector._update_stock_status()
    assert result["automatic_clear_count"] == 0
    after = await snapshot(factory)
    assert after["tag"]["is_suspended"] and after["tag"]["board_tag"] == "suspended"
    assert after["blacklist"]["start_date"] == DAY.isoformat()
    assert after["blacklist"]["reason"] == "suspended"


@pytest.mark.asyncio
async def test_wrapper_singleflight_and_finally(monkeypatch):
    collector = module.DataScheduler()
    entered, release = asyncio.Event(), asyncio.Event()
    async def merge():
        entered.set()
        await release.wait()
        return {"positive_merge_status": "ok"}
    monkeypatch.setattr(collector, "_merge_stock_risks", merge)
    verify = AsyncMock(return_value={"automatic_clear_count": 1})
    monkeypatch.setattr(collector, "_refresh_verified_trading_status", verify)
    task = asyncio.create_task(collector._update_stock_status())
    await entered.wait()
    assert (await collector._update_stock_status())["status"] == "skipped"
    release.set()
    assert (await task)["automatic_clear_count"] == 1
    assert not collector._stock_status_refreshing
    verify.assert_awaited_once()
    monkeypatch.setattr(collector, "_merge_stock_risks", AsyncMock(side_effect=asyncio.CancelledError))
    with pytest.raises(asyncio.CancelledError):
        await collector._update_stock_status()
    assert not collector._stock_status_refreshing
