"""Quote observation is not trading permission: real collector, isolated DB/HTTP."""
import json
from datetime import date, datetime
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.stock_tagger import stock_tagger
from app.data import scheduler as module
from app.data.limit_pool import limit_pool_health
from app.data.sources import tencent_source
from app.db.session import Base
from app.models.stock import LimitUpPool, QuoteRound, StockBlacklist, StockSpot, StockTag

DAY = date(2026, 9, 29)
NOW = datetime(2026, 9, 29, 16, 15)
SOURCE_AT = datetime(2026, 9, 29, 16, 14, 56)
OLD_AT = datetime(2026, 9, 16, 15, 4, 56)


class Clock(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW if tz is None else NOW.replace(tzinfo=tz)


@pytest_asyncio.fixture
async def isolated(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(module, "async_session", factory)
    monkeypatch.setattr(module, "datetime", Clock)
    monkeypatch.setattr(tencent_source, "datetime", Clock)
    monkeypatch.setattr(module.trade_calendar, "is_trade_day", AsyncMock(return_value=True))
    monkeypatch.setattr(module.trade_calendar, "is_trading_hours", AsyncMock(return_value=False))
    monkeypatch.setattr(module.trade_calendar, "get_trade_session", lambda *args: "after_close")
    # Only the request boundary is replaced; URL batching, parsing and both DB
    # projections run for real. Never call a live provider from this test file.
    async def no_http(*args, **kwargs):
        raise AssertionError("unexpected network request")
    def no_sync_http(*args, **kwargs):
        raise AssertionError("unexpected synchronous network request")
    monkeypatch.setattr(httpx.AsyncClient, "get", no_http)
    monkeypatch.setattr(httpx.Client, "get", no_sync_http)
    scheduler = module.DataScheduler()
    monkeypatch.setattr(scheduler, "_schedule_quote_round_archive", lambda payload: None)
    monkeypatch.setattr(scheduler, "_publish_quote_round", lambda payload: None)
    monkeypatch.setattr(scheduler, "_request_anomaly_scan", lambda reason: None)
    monkeypatch.setattr("app.signal.anomaly_scanner.anomaly_scanner.enqueue_quote_batch", lambda rows: None)
    try:
        yield factory, scheduler
    finally:
        await engine.dispose()


def tag(code, name="测试", *, board_tag=None, suspended=False, st=False, delisting=False):
    board = stock_tagger.get_board_type(code)
    return StockTag(
        code=code, name=name, board_type=board,
        board_tag=board_tag or stock_tagger.get_board_tag(board),
        is_suspended=suspended, is_st=st, is_delisting=delisting,
        updated_at=OLD_AT,
    )


async def seed_subject(factory):
    async with factory() as session:
        session.add(tag("600825", "新华传媒", board_tag="suspended", suspended=True))
        session.add(StockBlacklist(
            code="600825", reason="suspended", start_date=date(2026, 9, 17),
            end_date=None, auto_expire=False, source="auto",
        ))
        session.add(StockSpot(
            code="600825", name="新华传媒", price=5.31, prev_close=5.31,
            open=0, high=0, low=0, volume=0, limit_up=5.84, limit_down=4.78,
            source_quote_at=OLD_AT, received_at=OLD_AT, updated_at=OLD_AT,
        ))
        await session.commit()


async def frozen_risk_rows(factory):
    async with factory() as session:
        # Compare every persisted risk field, not only the boolean under test.
        frozen = []
        for model in (StockTag, StockBlacklist):
            rows = (await session.execute(
                select(*model.__table__.columns).order_by(model.code)
            )).all()
            frozen.append(tuple(tuple(row) for row in rows))
        return tuple(frozen)


def quote_payload(code, *, name="测试", source_at=SOURCE_AT, zero_volume=False):
    fields = ["0"] * 88
    for index, value in {
        1: name, 2: code, 3: 9.41, 4: 8.55, 5: 9.41, 6: 156515,
        9: 9.41, 10: 50000, 30: source_at.strftime("%Y%m%d%H%M%S"),
        32: 10.06, 33: 9.41, 34: 9.41, 38: 1.5, 44: 100,
        47: 9.41, 48: 7.70, 49: 1, 51: 9.41,
    }.items():
        fields[index] = str(value)
    if zero_volume:
        for index in (5, 6, 33, 34, 51):
            fields[index] = "0"
    prefix = "sh" if code.startswith("6") else "sz"
    return 'v_' + prefix + code + '="' + "~".join(fields) + '";'


def mock_quotes(monkeypatch, payloads):
    requested = []
    async def get(client, url, **kwargs):
        symbols = str(url).split("/q=", 1)[1].split(",")
        requested.extend(symbols)
        body = "\n".join(payloads[symbol[2:]] for symbol in symbols if symbol[2:] in payloads)
        return httpx.Response(200, text=body, request=httpx.Request("GET", url))
    monkeypatch.setattr(httpx.AsyncClient, "get", get)
    return requested


@pytest.mark.asyncio
async def test_opt_in_keeps_default_research_close_scope(isolated):
    factory, _ = isolated
    async with factory() as session:
        session.add_all([
            tag("600519"), tag("300750"),
            tag("600825", board_tag="suspended", suspended=True),
            tag("600001", board_tag="blocked"),
            tag("600002", board_tag="blocked", st=True),
            tag("600003", board_tag="suspended", suspended=True, st=True),
            tag("600004", board_tag="suspended"),  # stale tag with false flag
            tag("600005", suspended=True),  # true flag with normal board tag
            tag("600006", "旧退市风险", board_tag="blocked", delisting=True),
            tag("600007", "退市示例"), tag("600008", "示例退"),
            tag("920001"), tag("430047"), tag("830799"),
        ])
        await session.commit()
        default_codes = set(await session.scalars(select(StockTag.code).where(
            *module._research_universe_filters()
        )))
        observed_codes = set(await session.scalars(select(StockTag.code).where(
            *module._research_universe_filters(include_risk_blocked=True)
        )))
    assert len(module._research_universe_filters()) == 6
    assert default_codes == {"600519", "300750", "600002"}
    assert observed_codes == default_codes | {
        "600825", "600001", "600003", "600004", "600005", "600006",
    }


@pytest.mark.asyncio
async def test_real_collector_recovers_resumed_stock_without_clearing_risk(isolated, monkeypatch):
    factory, scheduler = isolated
    await seed_subject(factory)
    before = await frozen_risk_rows(factory)
    requested = mock_quotes(monkeypatch, {"600825": quote_payload("600825", name="新华传媒")})
    result = await scheduler._tencent_spot_collect(force=True)
    assert requested == ["sh600825"]
    assert result["collected"] == 1
    async with factory() as session:
        spot = await session.get(StockSpot, "600825")
        assert (spot.price, spot.limit_up, spot.volume) == (9.41, 9.41, 156515)
        assert spot.source_quote_at == SOURCE_AT and spot.updated_at == NOW
        pool = await session.scalar(select(LimitUpPool))
        assert pool.code == "600825" and pool.source == "tencent"
        assert pool.source_quote_at == SOURCE_AT
        assert pool.consecutive_days is None  # Cannot invent six boards from price.
        round_row = await session.scalar(select(QuoteRound))
        evidence = json.loads(round_row.component_watermarks_json)["limit_pool"]
        assert round_row.expected_count == evidence["expected_count"] == 1
        assert evidence["valid_count"] == evidence["up_count"] == 1
        health = await limit_pool_health(session, trade_date=DAY, decision_at=NOW, require_close=True)
        assert not health["ready"] and health["detail_unknown_count"] == 1
        assert await stock_tagger.filter_signals(session, [{"code": "600825", "name": "新华传媒"}]) == []
        status = await stock_tagger.load_status(session, "600825")
        assert status["is_suspended"] and status["active_blacklist"]
        assert status["is_tradeable"] is False and status["execution_authorized"] is False
    assert await frozen_risk_rows(factory) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["zero_volume", "stale", "missing"])
async def test_observation_does_not_turn_halted_stale_or_missing_into_valid_pool(
    isolated, monkeypatch, kind,
):
    factory, scheduler = isolated
    await seed_subject(factory)
    async with factory() as session:
        session.add(tag("600519"))
        await session.commit()
    before = await frozen_risk_rows(factory)
    payloads = {"600519": quote_payload("600519")}
    if kind != "missing":
        payloads["600825"] = quote_payload(
            "600825", name="新华传媒",
            source_at=OLD_AT if kind == "stale" else SOURCE_AT,
            zero_volume=kind == "zero_volume",
        )
    requested = mock_quotes(monkeypatch, payloads)
    await scheduler._tencent_spot_collect(force=True)
    assert set(requested) == {"sh600519", "sh600825"}
    async with factory() as session:
        assert set(await session.scalars(select(LimitUpPool.code))) == {"600519"}
        row = await session.scalar(select(QuoteRound))
        state = json.loads(row.component_watermarks_json)["limit_pool"]
        assert row.expected_count == state["expected_count"] == 2
        assert state["valid_count"] == 1 and state["coverage"] == .5
        health = await limit_pool_health(session, trade_date=DAY, decision_at=NOW, require_close=True)
        assert not health["ready"]
    assert await frozen_risk_rows(factory) == before


@pytest.mark.asyncio
async def test_collector_validates_codes_and_covers_blocked_not_only_suspended(isolated, monkeypatch):
    factory, scheduler = isolated
    codes = ["600825", "600001", "300750", "688111", "689009", "302132", "002058"]
    async with factory() as session:
        # 002058: sticky delisting-risk flag is not an actual delisted identity.
        session.add_all([
            tag(code, board_tag="blocked", delisting=code == "002058")
            for code in codes
        ])
        session.add_all([tag(code, board_tag="blocked") for code in
                         ["999999", "60082x", "60082", "6008257", "920001", "430047", "830799"]])
        session.add(tag("600007", "退市示例", board_tag="blocked"))
        session.add(tag("600008", "示例退", board_tag="blocked"))
        await session.commit()
    before = await frozen_risk_rows(factory)
    requested = mock_quotes(monkeypatch, {code: quote_payload(code) for code in codes})
    await scheduler._tencent_spot_collect(force=True)
    assert {symbol[2:] for symbol in requested} == set(codes)
    assert set(scheduler._tradeable_codes) == set(codes)
    async with factory() as session:
        row = await session.scalar(select(QuoteRound))
        assert row.expected_count == len(codes)
        assert await session.scalar(select(LimitUpPool.code).where(LimitUpPool.code == "002058")) == "002058"
        status = await stock_tagger.load_status(session, "002058")
        assert status["is_delisting"] and not status["is_st"]
        assert not status["is_tradeable"] and not status["execution_authorized"]
        assert await stock_tagger.filter_signals(session, [{"code": code} for code in codes]) == []
    assert await frozen_risk_rows(factory) == before


@pytest.mark.asyncio
async def test_empty_response_does_not_change_risk_or_create_round(isolated, monkeypatch):
    factory, scheduler = isolated
    await seed_subject(factory)
    before = await frozen_risk_rows(factory)
    requested = mock_quotes(monkeypatch, {})
    result = await scheduler._tencent_spot_collect(force=True)
    assert requested == ["sh600825"]
    assert result["reason"] == "empty_response"
    async with factory() as session:
        assert await session.scalar(select(QuoteRound.id)) is None
        assert await session.scalar(select(LimitUpPool.id)) is None
        assert (await session.get(StockSpot, "600825")).source_quote_at == OLD_AT
    assert await frozen_risk_rows(factory) == before


@pytest.mark.asyncio
async def test_pre_market_still_resets_cached_universe(isolated, monkeypatch):
    _, scheduler = isolated
    scheduler._tradeable_codes = ["600519"]
    monkeypatch.setattr(module.trade_calendar, "is_trade_day", AsyncMock(return_value=False))
    await scheduler._pre_market()
    assert scheduler._tradeable_codes == []
