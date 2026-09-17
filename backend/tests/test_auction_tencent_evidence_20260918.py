"""D 路由：用腾讯实时行情生成可认证的 09:25 竞价证据。

背景（实测）
------------
`auction_data` 全表 156 万行 `source_quote_at` 100% 为 NULL，
采集器走东财/新浪 `spot` 并把 `price_basis` 标成 `spot_open_unverified`
—— 契约 `auction_provenance_v1` 因此永远判 `unknown`，
`multi_frame_complete_count=0` → 闸门分子为 0
→ D 路由 `candidate_data_missing` 3,226 次、**零成交**。
而 `stock_spot`（腾讯）实测 5,224/5,231 行**有** provider 行情时间戳
（字段[30]），字段集也齐：open / prev_close / volume(手) / amount /
volume_ratio。所以腾讯路径可以把这一环补上。

契约依据
--------
`price_basis="auction_opening"` + `volume_basis="auction_matched"`
+ `source_quote_at.time() >= 09:25`（`auction_evidence_status` 的第二组分支）。
金融假设：09:25 撮合、09:25–09:30 无连续竞价成交，故该窗口内
今开=竞价价、累计量额=竞价量额；本仓库 `TRADE_SESSIONS` 也把
09:25–09:30 留空。**该假设显式写在方法 docstring 里。**

安全保证
--------
产出前逐行用**策略同一个** `auction_evidence_status` 自检，只写 `ok` 的行；
不可能伪造证据。以下测试锁死这一点。
"""
from __future__ import annotations

from datetime import date, datetime

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.data.auction_evidence import auction_evidence_status
from app.db.session import Base
from app.models.stock import AuctionData, StockTag
from app.strategy.auction import TENCENT_AUCTION_SOURCE_VERSION, AuctionCollector

DAY = date(2026, 9, 18)
AT = datetime(2026, 9, 18, 9, 25, 12)


@pytest_asyncio.fixture
async def maker(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'a.sqlite'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


def _quote(code="600000", *, source_at=None, **overrides):
    record = {
        "code": code, "name": "测试股",
        "open": 11.0, "prev_close": 10.0,
        "volume": 12_000, "amount": 13_200_000.0, "volume_ratio": 3.5,
        "source_quote_at": source_at or datetime(2026, 9, 18, 9, 25, 10),
        "received_at": datetime(2026, 9, 18, 9, 25, 11),
    }
    record.update(overrides)
    return record


def _patch(monkeypatch, records):
    async def fake(self, codes):
        return list(records)

    from app.data.sources.tencent_source import TencentSource

    monkeypatch.setattr(TencentSource, "collect_spot_batch", fake)


@pytest.mark.asyncio
async def test_outside_the_evidence_window_writes_nothing(maker, monkeypatch):
    _patch(monkeypatch, [_quote()])
    async with maker() as db:
        for moment in (datetime(2026, 9, 18, 9, 24, 59),
                       datetime(2026, 9, 18, 9, 25, 31),
                       datetime(2026, 9, 18, 10, 0, 0)):
            result = await AuctionCollector().collect_tencent_auction_evidence(
                db, DAY, now=moment, codes=["600000"],
            )
            assert result["status"] == "outside_evidence_window"
            assert result["written"] == 0
        assert (await db.scalars(select(AuctionData))).all() == []


@pytest.mark.asyncio
async def test_inside_window_writes_only_rows_that_pass_the_contract(maker, monkeypatch):
    _patch(monkeypatch, [
        _quote("600000"),                                              # 合格
        _quote("600001", source_at=datetime(2026, 9, 18, 9, 24, 59)),   # 早于 09:25 → 不能用 opening 分支
        _quote("600002", volume_ratio=0),                               # 量比 0 → incomplete_values
        _quote("600003", volume=0),                                     # 成交量为 0
        _quote("600004", open=0),                                       # 无开盘价
    ])
    async with maker() as db:
        result = await AuctionCollector().collect_tencent_auction_evidence(
            db, DAY, now=AT, codes=["600000", "600001", "600002", "600003", "600004"],
        )
        assert result["status"] == "ok"
        assert result["accepted"] == 1 and result["written"] == 1
        rows = (await db.scalars(select(AuctionData))).all()
        assert [r.code for r in rows] == ["600000"]

    row = rows[0]
    assert auction_evidence_status(row, decision_at=AT) == "ok"
    assert row.source == "tencent"
    assert row.source_version == TENCENT_AUCTION_SOURCE_VERSION
    assert row.price_basis == "auction_opening"
    assert row.volume_basis == "auction_matched"
    assert row.volume_unit == "lot100" and row.amount_unit == "CNY"
    assert row.source_quote_at == datetime(2026, 9, 18, 9, 25, 10)
    assert row.auction_time == "09:25:12"


@pytest.mark.asyncio
async def test_no_verified_rows_reports_why_and_writes_nothing(maker, monkeypatch):
    _patch(monkeypatch, [
        _quote("600000", volume_ratio=0),
        _quote("600001", source_at=datetime(2026, 9, 18, 9, 24, 0)),
    ])
    async with maker() as db:
        result = await AuctionCollector().collect_tencent_auction_evidence(
            db, DAY, now=AT, codes=["600000", "600001"],
        )
        assert result["status"] == "no_verified_rows"
        assert result["written"] == 0
        assert result["rejected"], "必须给出被拒原因分类"
        assert (await db.scalars(select(AuctionData))).all() == []


@pytest.mark.asyncio
async def test_repeated_sampling_builds_multiple_distinct_frames(maker, monkeypatch):
    """`multi_frame_complete_count` 是闸门分子：需要 >=2 个不同
    source_quote_at 的 ok 帧。同一标的在不同秒采样得到不同 provider
    时间戳时必须累积成两帧。"""

    async def run_at(moment, source_at):
        # 契约要求 source <= received <= observed，received_at 必须夹在中间
        _patch(monkeypatch, [_quote("600000", source_at=source_at, received_at=moment)])
        async with maker() as db:
            return await AuctionCollector().collect_tencent_auction_evidence(
                db, DAY, now=moment, codes=["600000"],
            )

    first = await run_at(datetime(2026, 9, 18, 9, 25, 6), datetime(2026, 9, 18, 9, 25, 5))
    second = await run_at(datetime(2026, 9, 18, 9, 25, 16), datetime(2026, 9, 18, 9, 25, 15))
    assert first["status"] == second["status"] == "ok"
    async with maker() as db:
        rows = (await db.scalars(select(AuctionData))).all()
        assert len(rows) == 2
        assert len({r.source_quote_at for r in rows}) == 2
        assert {r.auction_time for r in rows} == {"09:25:06", "09:25:16"}
        assert all(
            auction_evidence_status(r, decision_at=datetime(2026, 9, 18, 9, 25, 30)) == "ok"
            for r in rows
        )
        assert (await db.scalar(select(func.count()).select_from(AuctionData))) == 2


@pytest.mark.asyncio
async def test_universe_is_tradeable_non_st_non_suspended(maker, monkeypatch):
    """默认宇宙沿用 D 路由口径：主板 + 非 ST + 非停牌 + 非退市。"""
    captured = {}

    async def fake(self, codes):
        captured["codes"] = list(codes)
        return [_quote(c) for c in codes]

    from app.data.sources.tencent_source import TencentSource

    monkeypatch.setattr(TencentSource, "collect_spot_batch", fake)
    async with maker() as db:
        db.add_all([
            StockTag(code="600000", name="主板股", board_type="main_sh", board_tag="tradeable"),
            StockTag(code="300750", name="创业板", board_type="gem", board_tag="observe_only"),
            StockTag(code="600001", name="ST股", board_type="main_sh", board_tag="blocked", is_st=True),
            StockTag(code="600002", name="停牌股", board_type="main_sh", board_tag="suspended",
                     is_suspended=True),
        ])
        await db.commit()
        result = await AuctionCollector().collect_tencent_auction_evidence(db, DAY, now=AT)
        assert result["status"] == "ok"
        assert captured["codes"] == ["600000"]


def test_scheduler_registers_a_second_level_window_job():
    """整分 cron 覆盖不到 09:25:00–09:25:30 的多次采样，必须有秒级任务。"""
    from app.data.scheduler import DataScheduler

    scheduler = DataScheduler()
    scheduler.setup_jobs()
    job = scheduler.scheduler.get_job("auction_evidence_0925")
    assert job is not None
    trigger = str(job.trigger)
    assert "hour='9'" in trigger and "minute='25'" in trigger
    assert "second='5,15,25'" in trigger


@pytest.mark.asyncio
async def test_unverifiable_pre_0925_path_does_not_shadow_verified_frames(maker, monkeypatch):
    """东财/新浪路径在 >=09:25 写不出可认证帧，且它的 auction_time 若更晚
    会把腾讯已验证帧从"最新帧"里顶掉（get_snapshot_health 只取 MAX(auction_time)），
    导致分子重新归零。该路径必须让出这个窗口。"""
    import inspect

    from app.strategy.auction import AuctionCollector as Collector

    body = inspect.getsource(Collector.collect_auction_data)
    assert "if observed_at.time() >= time(9, 25):" in body
    assert "遮蔽" in body

    # 结构性验证：写一条 unverified 的 09:25:26 帧，确认它确实会抢走"最新帧"
    async with maker() as db:
        db.add_all([
            AuctionData(code="600000", trade_date=DAY, auction_time="09:25:12",
                        auction_price=11.0, auction_volume=12_000, auction_amount=13_200_000.0,
                        prev_close=10.0, volume_ratio=3.5, source="tencent",
                        source_version=TENCENT_AUCTION_SOURCE_VERSION,
                        source_quote_at=datetime(2026, 9, 18, 9, 25, 10),
                        received_at=datetime(2026, 9, 18, 9, 25, 11),
                        observed_at=datetime(2026, 9, 18, 9, 25, 12),
                        price_basis="auction_opening", volume_basis="auction_matched",
                        volume_unit="lot100", amount_unit="CNY"),
            AuctionData(code="600000", trade_date=DAY, auction_time="09:25:26",
                        auction_price=11.0, auction_volume=12_000, auction_amount=13_200_000.0,
                        prev_close=10.0, volume_ratio=3.5, source="sina",
                        source_version="akshare_spot_open_v1_unverified",
                        source_quote_at=None,
                        received_at=datetime(2026, 9, 18, 9, 25, 26),
                        observed_at=datetime(2026, 9, 18, 9, 25, 26),
                        price_basis="spot_open_unverified",
                        volume_basis="intraday_cumulative",
                        volume_unit="share", amount_unit="CNY"),
        ])
        await db.commit()
        health = await Collector().get_snapshot_health(
            db, DAY, as_of_at=datetime(2026, 9, 18, 9, 26),
        )
    # 最新帧是那条 unverified → 分子为 0，正是要避免的遮蔽
    assert health["feed_complete_count"] == 0
    assert health["evidence_status_counts"] == {"unknown": 1}
