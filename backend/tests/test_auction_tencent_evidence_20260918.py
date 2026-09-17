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


# ---------------------------------------------------------------------------
# 第二来源：东财 f124（09:25 无成交窗口里，单一来源的时间戳不会推进）
# ---------------------------------------------------------------------------

def _em_row(code, *, open_=11.0, prev=10.0, volume=12_000, amount=13_200_000.0,
            vr=3.5, f124=1_789_648_500):
    return {"f12": code, "f14": "测试股", "f17": open_, "f18": prev,
            "f5": volume, "f6": amount, "f10": vr, "f124": f124}


def _patch_em(monkeypatch, rows, *, fail_first_host=False):
    from app.strategy import auction as auction_module

    calls = {"hosts": []}

    class _Resp:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    class _Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, params=None, headers=None):
            host = url.split("/")[2]
            calls["hosts"].append(host)
            if fail_first_host and host == auction_module.EASTMONEY_QUOTE_HOSTS[0]:
                raise RuntimeError("host down")
            wanted = {s.split(".")[1] for s in (params or {}).get("secids", "").split(",") if s}
            return _Resp({"data": {"diff": [r for r in rows if r["f12"] in wanted]}})

    monkeypatch.setattr("httpx.AsyncClient", _Client)
    return calls


@pytest.mark.asyncio
async def test_eastmoney_source_writes_only_contract_ok_rows(maker, monkeypatch):
    # f124 = 2026-09-18 09:25:12 (+08) 附近的 Unix 秒
    stamp = int(datetime(2026, 9, 18, 9, 25, 8).timestamp())
    _patch_em(monkeypatch, [
        _em_row("600000", f124=stamp),
        _em_row("600001", vr=0, f124=stamp),      # 量比 0 → incomplete_values
        _em_row("600002", f124=stamp, open_=0),   # 无开盘价
    ])
    async with maker() as db:
        result = await AuctionCollector().collect_eastmoney_auction_evidence(
            db, DAY, now=AT, codes=["600000", "600001", "600002"],
        )
        assert result["status"] == "ok" and result["written"] == 1
        rows = (await db.scalars(select(AuctionData))).all()
        assert [r.code for r in rows] == ["600000"]
    row = rows[0]
    assert row.source == "eastmoney"
    assert row.volume_unit == "lot100" and row.amount_unit == "CNY"
    assert row.price_basis == "auction_opening" and row.volume_basis == "auction_matched"
    assert row.source_quote_at == datetime(2026, 9, 18, 9, 25, 8)
    assert auction_evidence_status(row, decision_at=AT) == "ok"


@pytest.mark.asyncio
async def test_eastmoney_falls_back_to_second_host(maker, monkeypatch):
    from app.strategy.auction import EASTMONEY_QUOTE_HOSTS

    stamp = int(datetime(2026, 9, 18, 9, 25, 8).timestamp())
    calls = _patch_em(monkeypatch, [_em_row("600000", f124=stamp)], fail_first_host=True)
    async with maker() as db:
        result = await AuctionCollector().collect_eastmoney_auction_evidence(
            db, DAY, now=AT, codes=["600000"],
        )
        assert result["status"] == "ok" and result["written"] == 1
    assert EASTMONEY_QUOTE_HOSTS[0] in calls["hosts"]
    assert EASTMONEY_QUOTE_HOSTS[1] in calls["hosts"]


@pytest.mark.asyncio
async def test_two_sources_satisfy_the_two_frame_requirement(maker):
    """**D 路由能否解锁的判定性测试**：两个来源给出不同 source_quote_at，
    使同一只代码拥有 >=2 个 ok 帧，`multi_frame_complete_count` 达标、
    `path_degraded=False`，闸门分子才可能到 0.95。"""
    async with maker() as db:
        db.add(StockTag(code="600000", name="浦发银行", board_type="main_sh",
                        board_tag="tradeable"))
        db.add_all([
            AuctionData(code="600000", trade_date=DAY, auction_time="09:25:08",
                        auction_price=11.0, auction_volume=12_000,
                        auction_amount=13_200_000.0, prev_close=10.0, volume_ratio=3.5,
                        source="tencent", source_version=TENCENT_AUCTION_SOURCE_VERSION,
                        source_quote_at=datetime(2026, 9, 18, 9, 25, 5),
                        received_at=datetime(2026, 9, 18, 9, 25, 7),
                        observed_at=datetime(2026, 9, 18, 9, 25, 8),
                        price_basis="auction_opening", volume_basis="auction_matched",
                        volume_unit="lot100", amount_unit="CNY"),
            AuctionData(code="600000", trade_date=DAY, auction_time="09:25:18",
                        auction_price=11.0, auction_volume=12_000,
                        auction_amount=13_200_000.0, prev_close=10.0, volume_ratio=3.5,
                        source="eastmoney", source_version="eastmoney_qt_auction_open_v1",
                        source_quote_at=datetime(2026, 9, 18, 9, 25, 15),
                        received_at=datetime(2026, 9, 18, 9, 25, 17),
                        observed_at=datetime(2026, 9, 18, 9, 25, 18),
                        price_basis="auction_opening", volume_basis="auction_matched",
                        volume_unit="lot100", amount_unit="CNY"),
        ])
        await db.commit()
        health = await AuctionCollector().get_snapshot_health(
            db, DAY, as_of_at=datetime(2026, 9, 18, 9, 26),
        )

    assert health["feed_complete_count"] == 1
    assert health["multi_frame_complete_count"] == 1
    assert health["multi_frame_complete_ratio"] == 1.0
    assert health["verified_timely_snapshot_ratio"] == 1.0
    assert health["path_degraded"] is False
    assert health["field_degraded"] is False
    assert health["degraded"] is False
    # 闸门口径（prediction_data_quality）：count=multi_frame_complete_count,
    # expected=可交易宇宙 → completeness = min(field_coverage, timely_ratio) = 1.0
    assert min(health["multi_frame_complete_count"] / 1, health["verified_timely_snapshot_ratio"]) == 1.0


@pytest.mark.asyncio
async def test_single_frame_still_fails_the_two_frame_requirement(maker):
    """反向对照：只有一个来源的帧时必须仍判 `path_degraded=True`
    —— 证明这个测试不是在自我安慰。"""
    async with maker() as db:
        db.add(StockTag(code="600000", name="浦发银行", board_type="main_sh",
                        board_tag="tradeable"))
        db.add(AuctionData(
            code="600000", trade_date=DAY, auction_time="09:25:08",
            auction_price=11.0, auction_volume=12_000, auction_amount=13_200_000.0,
            prev_close=10.0, volume_ratio=3.5,
            source="tencent", source_version=TENCENT_AUCTION_SOURCE_VERSION,
            source_quote_at=datetime(2026, 9, 18, 9, 25, 5),
            received_at=datetime(2026, 9, 18, 9, 25, 7),
            observed_at=datetime(2026, 9, 18, 9, 25, 8),
            price_basis="auction_opening", volume_basis="auction_matched",
            volume_unit="lot100", amount_unit="CNY"))
        await db.commit()
        health = await AuctionCollector().get_snapshot_health(
            db, DAY, as_of_at=datetime(2026, 9, 18, 9, 26),
        )
    assert health["feed_complete_count"] == 1
    assert health["multi_frame_complete_count"] == 0
    assert health["path_degraded"] is True
    assert health["degraded"] is True


def test_scheduler_samples_both_sources_in_the_window():
    import inspect

    from app.data.scheduler import DataScheduler

    body = inspect.getsource(DataScheduler._auction_collect_tencent_evidence)
    assert "collect_tencent_auction_evidence" in body
    assert "collect_eastmoney_auction_evidence" in body
