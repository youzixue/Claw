"""Read-only page contracts: no gap-crossing, fake zeros or hidden ladder members."""
from datetime import date, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from app.api.v1 import promotion
from app.data.limit_pool import supplement_wencai_limit_details
from app.models.stock import BrokenLimitPool, LimitUpPool
from test_limit_pool_pipeline_20260928 import db, save, spot, DETAIL

DAY = date(2026, 9, 29)
CLOSE = datetime(2026, 9, 29, 15, 0, 1)


@pytest.fixture(autouse=True)
def market_clock(monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return CLOSE + timedelta(seconds=20)
    monkeypatch.setattr(promotion, "datetime", Clock)
    start = date(2026, 1, 1)
    monkeypatch.setattr(promotion.trade_calendar, "_cache", {
        start + timedelta(days=i): (start + timedelta(days=i)).weekday() < 5
        for i in range(365)
    })


async def complete(db, codes=("600001",), *, broken=(), at=CLOSE, breaks=0):
    await save(db, [
        *[spot(code, source_at=at) for code in codes],
        *[spot(code, price=10.8, source_at=at) for code in broken],
    ], at=at)
    await supplement_wencai_limit_details(
        db, {code: {**DETAIL, "break_count": breaks} for code in codes},
        requested_at=at, observed_at=at + timedelta(seconds=10),
    )
    await db.commit()


@pytest.mark.asyncio
async def test_all_members_and_resealed_stocks_have_distinct_rate_meaning(db, monkeypatch):
    codes = tuple(f"60000{i}" for i in range(1, 8))
    await complete(db, codes, broken=("600008",), breaks=1)
    # New-source pools are already verified. A later StockSpot must not
    # silently change only the chart but leave the ladder on another snapshot.
    monkeypatch.setattr(promotion, "_load_spot_map", AsyncMock(side_effect=AssertionError("cross-snapshot spot")))
    ladder = await promotion.promotion_ladder(db)
    board = await promotion.board_height(db)
    assert ladder["status"] == board["status"] == "ok"
    assert ladder["trade_date"] == board["trade_date"] == str(DAY)
    assert ladder["ladder"][0]["count"] == len(ladder["ladder"][0]["stocks"]) == 7
    assert ladder["ladder"][0]["seal_rate"] == 0
    assert ladder["seal_rate_method"] == "zero_break_share"
    assert board["limit_up_count"] == 7
    assert board["touched_limit_up_count"] == 8
    assert board["broken_limit_count"] == 1
    assert board["seal_rate"] == 87.5
    assert board["seal_rate_method"] == "verified_pool_state"


@pytest.mark.asyncio
async def test_previous_pool_gap_never_compares_to_older_pool(db):
    db.add(LimitUpPool(code="600001", name="测试", trade_date=date(2026, 9, 24), consecutive_days=1))
    await db.commit()
    await complete(db)
    result = await promotion.board_height(db)
    assert result["previous_trade_date"] == "2026-09-28"
    assert result["promotion_rate"] is None
    assert result["promoted_count"] is None
    assert result["promotion_rate_status"] == "previous_pool_missing_or_incomplete"


@pytest.mark.asyncio
async def test_previous_incomplete_new_pool_cannot_be_zero_or_partial_denominator(db):
    previous = CLOSE - timedelta(days=1)
    await save(db, [spot(source_at=previous)], at=previous)
    await complete(db)
    result = await promotion.board_height(db)
    assert result["previous_source_health"]["ready"] is False
    assert result["promotion_rate"] is None and result["promoted_count"] is None


@pytest.mark.asyncio
async def test_incomplete_source_blocks_both_views_instead_of_partial_ladder(db):
    await save(db, [spot(source_at=CLOSE)], at=CLOSE)
    ladder = await promotion.promotion_ladder(db)
    board = await promotion.board_height(db)
    assert ladder["status"] == board["status"] == "source_incomplete"
    assert ladder["source_health"]["ready"] is False
    assert ladder["ladder"] == []
    assert board["height"] is None and board["limit_up_count"] is None


@pytest.mark.asyncio
async def test_zero_market_uses_verified_current_date_not_old_pool(db):
    db.add(LimitUpPool(code="600001", name="历史", trade_date=date(2026, 9, 28), consecutive_days=3))
    await db.commit()
    await save(db, [spot(price=10.5, high=10.6, source_at=CLOSE)], at=CLOSE)
    ladder = await promotion.promotion_ladder(db)
    board = await promotion.board_height(db)
    assert ladder["trade_date"] == board["trade_date"] == str(DAY)
    assert ladder["status"] == "ok" and ladder["ladder"] == []
    assert board["height"] == board["limit_up_count"] == 0
    assert board["promotion_rate"] == 0
    assert board["seal_rate"] is None  # no touched stocks: undefined denominator


@pytest.mark.asyncio
async def test_historical_verified_zero_without_any_pool_keeps_its_date(db):
    previous = CLOSE - timedelta(days=1)
    await save(db, [spot(price=10.5, high=10.6, source_at=previous)], at=previous)
    board = await promotion.board_height(db)
    assert board["trade_date"] == "2026-09-28"
    assert board["status"] == "ok" and board["height"] == 0


@pytest.mark.asyncio
async def test_empty_database_is_missing_not_verified_zero(db):
    ladder = await promotion.promotion_ladder(db)
    board = await promotion.board_height(db)
    assert ladder["status"] == board["status"] == "missing"
    assert board["height"] is None and board["limit_up_count"] is None


@pytest.mark.asyncio
async def test_future_only_dirty_pool_is_not_published(db):
    db.add(LimitUpPool(code="600001", name="未来脏数据", trade_date=DAY + timedelta(days=1)))
    await db.commit()
    board = await promotion.board_height(db)
    assert board["trade_date"] == str(DAY)
    assert board["status"] == "missing" and board["height"] is None


@pytest.mark.asyncio
async def test_known_empty_previous_denominator_is_not_zero_percent(db):
    previous = CLOSE - timedelta(days=1)
    await save(db, [spot(price=10.5, high=10.6, source_at=previous)], at=previous)
    await complete(db)
    board = await promotion.board_height(db)
    assert board["previous_trade_date"] == "2026-09-28"
    assert board["promoted_count"] == 0 and board["promotion_rate"] is None
    assert board["promotion_rate_status"] == "previous_pool_empty"


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["stale", "future", "wrong_version", "count_mismatch"])
async def test_broken_denominator_requires_complete_current_evidence(db, invalid):
    from sqlalchemy import select
    await complete(db, broken=("600002",))
    row = await db.scalar(select(BrokenLimitPool))
    if invalid == "stale":
        row.source_quote_at = CLOSE.replace(hour=14)
    elif invalid == "future":
        row.observed_at = CLOSE + timedelta(days=1)
    elif invalid == "wrong_version":
        row.source_version = "unknown"
    else:
        db.add(BrokenLimitPool(code="600003", name="未知", trade_date=DAY))
    await db.commit()
    board = await promotion.board_height(db)
    assert board["limit_up_count"] == 1
    assert board["touched_limit_up_count"] is None
    assert board["seal_rate"] is None and board["broken_limit_count"] is None


@pytest.mark.asyncio
async def test_filter_scope_is_identical_for_up_and_broken_counts(db):
    from sqlalchemy import select
    await complete(db, ("600001", "600002", "301190"), broken=("600003", "600004"))
    up_st = await db.scalar(select(LimitUpPool).where(LimitUpPool.code == "600002"))
    broken_st = await db.scalar(select(BrokenLimitPool).where(BrokenLimitPool.code == "600004"))
    up_st.name = broken_st.name = "ST测试"
    await db.commit()
    board = await promotion.board_height(db)
    ladder = await promotion.promotion_ladder(db)
    assert board["limit_up_count"] == 2 and board["broken_limit_count"] == 1
    assert board["seal_rate"] == 66.7
    stocks = ladder["ladder"][0]["stocks"]
    assert len(stocks) == ladder["ladder"][0]["count"] == 2
    observer = next(stock for stock in stocks if stock["code"] == "301190")
    assert observer["is_tradeable"] is False and observer["tag"]


@pytest.mark.asyncio
async def test_new_heights_do_not_query_legacy_history(db, monkeypatch):
    await complete(db)
    normalize = AsyncMock(return_value={})
    monkeypatch.setattr(promotion, "_normalize_limit_up_consecutive_days", normalize)
    rows = await promotion._load_filtered_limit_ups(db, DAY)
    assert rows[0]["consecutive_days"] == 1
    assert normalize.call_args.args[2] == []


@pytest.mark.asyncio
async def test_resumed_high_board_is_visible_without_clearing_risk_or_entry_gate(db):
    from sqlalchemy import select
    from app.models.stock import StockTag, StockBlacklist

    db.add_all([
        StockTag(code="600825", name="测试", board_type="main_sh", board_tag="suspended",
                 is_st=False, is_suspended=True, is_delisting=False),
        StockBlacklist(code="600825", reason="suspended", start_date=date(2026, 9, 17),
                       auto_expire=False, source="auto"),
    ])
    await db.commit()
    await complete(db, ("600825", "603949"), broken=("600301",))
    for code, height in (("600825", 6), ("603949", 4)):
        row = await db.scalar(select(LimitUpPool).where(LimitUpPool.code == code))
        row.consecutive_days = height
    db.add(StockTag(code="600301", name="测试", board_type="main_sh", board_tag="suspended",
                    is_st=False, is_suspended=True, is_delisting=False))
    await db.commit()

    assert "600825" not in {r["code"] for r in await promotion._load_filtered_limit_ups(db, DAY)}
    board = await promotion.board_height(db)
    ladder = await promotion.promotion_ladder(db)
    assert board["height"] == 6 and board["leader"]["code"] == "600825"
    assert board["limit_up_count"] == 2 and board["broken_limit_count"] == 1
    assert board["seal_rate"] == 66.7
    assert board["scope"] == ladder["scope"] == "non_st_market_with_risk_annotations"
    leader = ladder["ladder"][0]["stocks"][0]
    assert leader["code"] == "600825" and leader["is_tradeable"] is False
    assert "标签待核验" in leader["tag"] and "禁止交易" in leader["tag"]
    tag = await db.get(StockTag, "600825")
    blacklist = await db.get(StockBlacklist, "600825")
    assert tag.is_suspended and tag.board_tag == "suspended"
    assert blacklist.end_date is None and blacklist.auto_expire is False


@pytest.mark.asyncio
async def test_market_display_preserves_manual_and_delisting_risk_bans_but_excludes_st(db):
    from app.models.stock import StockTag, StockBlacklist
    await complete(db, ("600001", "600002", "600003"))
    db.add_all([
        StockTag(code="600001", name="测试", board_type="main_sh", board_tag="tradeable"),
        StockBlacklist(code="600001", reason="manual", source="manual", start_date=DAY),
        StockTag(code="600002", name="测试", board_type="main_sh", board_tag="blocked", is_st=True),
        StockTag(code="600003", name="测试", board_type="main_sh", board_tag="blocked", is_delisting=True),
    ])
    await db.commit()
    ladder = await promotion.promotion_ladder(db)
    stocks = [s for level in ladder["ladder"] for s in level["stocks"]]
    assert {s["code"] for s in stocks} == {"600001", "600003"}
    assert all(s["is_tradeable"] is False for s in stocks)
    assert "退市风险标签" in next(s["tag"] for s in stocks if s["code"] == "600003")
    assert await promotion._load_filtered_limit_ups(db, DAY) == []


@pytest.mark.asyncio
async def test_halted_stock_without_a_valid_current_quote_cannot_raise_height(db):
    from app.models.stock import StockTag
    db.add(StockTag(code="600825", name="测试", board_type="main_sh",
                    board_tag="suspended", is_suspended=True))
    await db.commit()
    await save(db, [spot("600825", source_at=CLOSE - timedelta(days=1)),
                    spot("603949", source_at=CLOSE)], at=CLOSE, expected=2)
    board = await promotion.board_height(db)
    assert board["status"] == "source_incomplete" and board["height"] is None

