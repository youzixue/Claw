"""Missing optional Wencai reasons must not hide required board-height evidence.

Offline source-contract regression for the 2026-09-29 live query comparison.
No production DB, network, order submission, or weakened freshness thresholds.
"""
from datetime import date, datetime, timedelta
from unittest.mock import AsyncMock
import json
import socket

import pandas as pd
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.data.limit_pool import limit_pool_health
from app.data.scheduler import _calculate_market_sentiment_state
from app.models.stock import LimitUpPool
from test_limit_pool_pipeline_20260928 import DAY, NOW, db, save, spot


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("network forbidden in sentiment regression")
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)


def source_row(code, *, height, reason):
    return {
        "股票代码": code, "连续涨停天数[20260928]": height,
        "首次涨停时间[20260928]": "09:35:00",
        "涨停开板次数[20260928]": 0,
        "涨停封单额[20260928]": 80_000_000,
        "涨停原因[20260928]": reason,
    }


async def refresh(db, monkeypatch, rows, *, omit_reason_column=False):
    from app.data import scheduler as module

    class ClockMeta(type):
        def __instancecheck__(cls, value):
            # SQLite materializes builtin datetime, even with a frozen test clock.
            return isinstance(value, datetime)

    class Clock(datetime, metaclass=ClockMeta):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 9, 28, 10, 0, 10)

    seen = []

    async def query(_self, question, *, perpage):
        seen.append(question)
        assert perpage == 1000
        assert question.startswith("2026年09月28日涨停 ")
        # Live upstream interprets the optional reason clause as a non-empty
        # filter, not merely a projection. Preserve that failure in the fixture.
        selected = [r for r in rows if r["涨停原因[20260928]"]] if "涨停原因" in question else rows
        frame = pd.DataFrame(selected)
        if omit_reason_column:
            frame = frame.drop(columns=["涨停原因[20260928]"])
        frame.attrs.update(code_count=len(frame), row_count=len(frame))
        return frame

    monkeypatch.setattr(module, "datetime", Clock)
    monkeypatch.setattr(module, "async_session", async_sessionmaker(db.bind, expire_on_commit=False))
    monkeypatch.setattr(module.WencaiStreamSource, "query_async", query)
    monkeypatch.setattr(module.trade_calendar, "is_trade_day", AsyncMock(return_value=True))
    monkeypatch.setattr(module.data_quality_guard, "record_success", AsyncMock())
    monkeypatch.setattr(module.data_quality_guard, "record_failure", AsyncMock())
    scheduler = module.DataScheduler()
    result = await scheduler._intraday_fast(force=True)
    assert len(seen) == 1
    assert scheduler._limit_detail_task is None
    assert scheduler._limit_detail_refreshing is False
    db.expire_all()
    return result


def sentiment(height):
    return _calculate_market_sentiment_state(
        limit_up_count=40, limit_down_count=5, broken_limit_count=8,
        seal_rate=83.3, board_height=height, main_net_inflow=20.0,
        advance_decline_ratio=1.2, breadth_sample_count=2900,
        breadth_coverage=0.98, index_avg_change_pct=0.2,
        index_sample_count=3, turnover_total=1.2, fund_flow_coverage=0.98,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("omit_reason_column", [False, True])
async def test_optional_reason_does_not_filter_highest_board(db, monkeypatch, omit_reason_column):
    await save(db, [spot("600001"), spot("000001")])
    rows = [source_row("600001.SH", height=2, reason="已发布原因"),
            source_row("000001.SZ", height=4, reason=None)]
    result = await refresh(db, monkeypatch, rows, omit_reason_column=omit_reason_column)
    assert result == {"status": "ok", "updated": 2, "returned": 2}
    pools = list(await db.scalars(select(LimitUpPool).order_by(LimitUpPool.code)))
    assert [r.consecutive_days for r in pools] == [4, 2]
    assert pools[0].limit_up_reason is None
    assert pools[1].limit_up_reason == (None if omit_reason_column else "已发布原因")
    evidence = json.loads(pools[0].evidence_json)["wencai"]
    assert "consecutive_days" in evidence["known_fields"]
    assert "limit_up_reason" not in evidence["known_fields"]
    assert pools[0].source_quote_at == NOW  # Metadata never invents a quote clock.
    health = await limit_pool_health(db, trade_date=DAY, decision_at=NOW + timedelta(seconds=10))
    assert health["ready"] and health["detail_complete_count"] == 2
    assert sentiment(max(r.consecutive_days for r in pools))["quality_status"] == "ok"


@pytest.mark.asyncio
@pytest.mark.parametrize("column", [
    "连续涨停天数[20260928]", "首次涨停时间[20260928]",
    "涨停开板次数[20260928]", "涨停封单额[20260928]",
])
async def test_missing_required_metadata_still_blocks(db, monkeypatch, column):
    await save(db, [spot("600001")])
    row = source_row("600001.SH", height=2, reason=None)
    row[column] = None
    assert (await refresh(db, monkeypatch, [row]))["status"] == "ok"
    health = await limit_pool_health(db, trade_date=DAY, decision_at=NOW + timedelta(seconds=10))
    assert not health["ready"] and health["detail_unknown_count"] == 1
    if column == "连续涨停天数[20260928]":
        pool = await db.scalar(select(LimitUpPool))
        assert pool.consecutive_days is None
        assert sentiment(pool.consecutive_days)["quality_status"] == "degraded"


@pytest.mark.asyncio
async def test_missing_reason_does_not_extend_metadata_ttl(db, monkeypatch):
    from app.config.settings import settings
    await save(db, [spot("600001")])
    await refresh(db, monkeypatch, [source_row("600001.SH", height=3, reason=None)])
    later = NOW + timedelta(seconds=10 + settings.LIMIT_POOL_WENCAI_INTERVAL_SEC + 1)
    await save(db, [spot("600001", source_at=later)], at=later)
    pool = await db.scalar(select(LimitUpPool))
    assert pool.consecutive_days is None
    health = await limit_pool_health(db, trade_date=DAY, decision_at=later)
    assert not health["ready"] and health["detail_unknown_count"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_height", [False, True])
async def test_real_sentiment_snapshot_and_risk_consume_repaired_metadata(db, monkeypatch, missing_height):
    from app.data import scheduler as module
    from app.models.stock import FundFlow, MarketSentiment, StockSpot, StockTag
    from app.risk.engine import RiskContext, RiskLevel
    from app.risk.rules import SentimentCircuitBreakerRule

    # All fixtures are synthetic and confined to the existing temporary DB.
    codes = ["600001", "000001"] + [f"600{index:03d}" for index in range(2, 500)]
    for index, code in enumerate(codes):
        db.add(StockTag(code=code, board_type="main", board_tag="tradeable",
                        is_st=False, is_suspended=False, is_delisting=False))
        db.add(StockSpot(code=code, price=10, volume=100,
                         change_pct=1 if index % 2 else -1, updated_at=NOW))
        db.add(FundFlow(code=code, trade_date=DAY, main_net_inflow=1_000_000,
                        main_net_inflow_pct=1, source="tencent",
                        source_version="tencent_hsfundtab_v1", source_quote_at=NOW,
                        received_at=NOW, observed_at=NOW))
    await db.commit()
    await save(db, [spot("600001"), spot("000001")])
    rows = [source_row("600001.SH", height=2, reason="已发布"),
            source_row("000001.SZ", height=None if missing_height else 4, reason=None)]
    await refresh(db, monkeypatch, rows)

    class Day(date):
        @classmethod
        def today(cls):
            return DAY

    monkeypatch.setattr(module, "date", Day)
    monkeypatch.setattr(module.trade_calendar, "get_trade_session", lambda: "morning")
    scheduler = module.DataScheduler()
    scheduler._sources["index"] = type("IndexFixture", (), {
        "get_index_spot": AsyncMock(return_value=pd.DataFrame([
            {"代码": code, "最新价": 100.2, "今开": 100, "最高": 101, "最低": 99,
             "成交量": 1_000_000, "成交额": 1_000_000_000_000, "昨收": 100, "涨跌幅": 0.2}
            for code in ("000001", "399001", "399006")
        ])),
    })()
    result = await scheduler._intraday_indices_and_sentiment()
    expected_quality = "degraded" if missing_height else "ok"
    assert result == {"status": expected_quality, "snapshot_persisted": True}
    db.expire_all()
    snapshot = await db.scalar(select(MarketSentiment).where(MarketSentiment.trade_date == DAY))
    assert snapshot.board_height == (None if missing_height else 4)
    assert snapshot.quality_status == expected_quality
    decision = SentimentCircuitBreakerRule().check(RiskContext(
        code="002635", action="buy", is_paper_experiment=True, sentiment_required=True,
        sentiment_quality_status=snapshot.quality_status,
        sentiment_quality_reason=snapshot.quality_reason,
        sentiment_cycle=snapshot.sentiment_cycle, sentiment_score=snapshot.sentiment_score,
    ))
    assert decision.level == (RiskLevel.BLOCK if missing_height else RiskLevel.PASS)
