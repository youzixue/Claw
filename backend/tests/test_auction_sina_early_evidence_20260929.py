"""D2 早中段可认证竞价证据（新浪 hq 实时行情）。

背景（2026-09-29 实测与库内复核）
--------------------------------
`auction_data` 里 09:15–09:25 的帧此前只有新浪**行情中心**观察行：
`source_quote_at` 为空、`auction_volume` 全为 0、status 恒 unknown
（9/28 存下 1,520 行，全部如此）。D2 的三段判据里 `verified_early` 与
`cancel_phase_positive_samples` 因此永远为空。

`hq.sinajs.cn` 的响应带 provider 日期+时间，可产出**同一套契约**下的
可认证帧。以下测试锁死：

1. 窗口外（09:14:59 / 09:25:31）一律不写；
2. 只有"日期列==交易日 + 时间列可解析"的行才有时钟，**绝不用本机日期补造**；
3. >=09:25 的帧必须让给腾讯/东财路径，不在这条路径写；
4. 逐行自检不通过的行（无量/无价/无时钟）不写；
5. 同一轮内不同 source_quote_at 会累积成多帧（D2 不可撤单段需要 >=2 个）；
6. 数据源标签与单位显式（share / CNY），不与腾讯的 lot100 混用。
"""
from __future__ import annotations

from datetime import date, datetime, time

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.data.auction_evidence import auction_evidence_status
from app.db.session import Base
from app.models.stock import AuctionData, StockKline, StockTag
from app.strategy.auction import SINA_AUCTION_SOURCE_VERSION, AuctionCollector

DAY = date(2026, 9, 29)
# 竞价量120万股；历史均量1,200万股 → 既有公式0.1×20 = 2.0
AUCTION_SHARES = 1_200_000
AVG_DAILY_SHARES = 12_000_000


@pytest_asyncio.fixture
async def maker(tmp_path, request):
    # 每个用例一个库文件：同模块共用 tmp_path 时，同名库会把上一个用例的行带进来。
    safe = "".join(
        ch if ch.isalnum() else "_"
        for ch in f"{request.node.module.__name__}_{request.node.name}"
    )[:120]
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / (safe + '.sqlite')}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


async def _seed_kline(db, codes: list[str]):
    """写入 5 日均量历史 —— 量比换算的真实输入（不是测试替身）。幂等。"""
    from datetime import timedelta

    existing = {
        code for (code,) in (await db.execute(
            select(StockKline.code).where(StockKline.code.in_(codes))
        )).all()
    }
    pending = [code for code in codes if code not in existing]
    if not pending:
        return
    db.add_all([
        StockKline(code=code, trade_date=DAY - timedelta(days=offset + 1), volume=AVG_DAILY_SHARES)
        for code in pending for offset in range(5)
    ])
    await db.commit()


def _sina_payload(rows: list[dict]) -> str:
    """构造与实测响应同构的 34 字段文本（逗号分隔，行尾带分号）。"""
    lines = []
    for row in rows:
        fields = [""] * 34
        fields[0] = row.get("name", "测试股")
        fields[1] = str(row.get("open", "0.000"))
        fields[2] = str(row.get("prev_close", "10.000"))
        fields[3] = str(row.get("current", "0.000"))          # 竞价期恒为 0
        fields[6] = str(row.get("bid_price", "10.500"))       # 买一价 = 虚拟参考价
        fields[7] = str(row.get("ask_price", "10.500"))       # 卖一价 = 虚拟参考价
        fields[8] = str(row.get("volume", "0"))               # 竞价期恒为 0
        fields[9] = str(row.get("amount", "0.000"))           # 竞价期恒为 0
        fields[10] = str(row.get("bid_volume", "0"))          # 买一量 = 虚拟匹配量(股)
        fields[20] = str(row.get("ask_volume", "0"))          # 卖一量 = 虚拟匹配量(股)
        fields[30] = str(row.get("date", DAY.isoformat()))
        fields[31] = str(row.get("time", "09:18:03"))
        fields[32] = "00"
        prefix = "sh" if str(row["code"]).startswith("6") else "sz"
        lines.append(f'var hq_str_{prefix}{row["code"]}="{",".join(fields)}";')
    return "\n".join(lines)


def _patch(monkeypatch, rows: list[dict], *, status_code: int = 200, raises: bool = False,
           received_at=datetime(2026, 9, 29, 9, 18, 6)):
    """冻结响应墙钟；now 参数不能代替真实接收/观测时刻。"""
    import app.strategy.auction as module

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return received_at

    monkeypatch.setattr(module, "datetime", Clock)
    captured: dict = {"urls": []}

    class _Resp:
        def __init__(self, text: str, code: int):
            self.text = text
            self.status_code = code

        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(f"HTTP {self.status_code}")

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, params=None, headers=None):
            captured["urls"].append(url)
            if raises:
                raise RuntimeError("network down")
            return _Resp(_sina_payload(rows), status_code)

    monkeypatch.setattr("httpx.AsyncClient", _Client)
    return captured


@pytest.mark.asyncio
async def test_outside_window_writes_nothing(maker, monkeypatch):
    _patch(monkeypatch, [{"code": "600000"}])
    async with maker() as db:
        for moment in (datetime(2026, 9, 29, 9, 14, 59),
                       datetime(2026, 9, 29, 9, 25, 31),
                       datetime(2026, 9, 29, 10, 0, 0)):
            result = await AuctionCollector().collect_sina_auction_evidence(
                db, DAY, now=moment, codes=["600000"],
            )
            assert result["status"] == "outside_evidence_window"
            assert result["written"] == 0
        assert (await db.scalars(select(AuctionData))).all() == []


@pytest.mark.asyncio
async def test_only_rows_with_same_day_source_clock_are_accepted(maker, monkeypatch):
    """日期列不是当日（盘前仍是上一交易日的快照）时不得用本机日期补造源钟。"""
    _patch(monkeypatch, [
        {"code": "600000", "bid_volume": AUCTION_SHARES, "ask_volume": AUCTION_SHARES,
         "bid_price": 10.5, "ask_price": 10.5, "prev_close": 10.0,
         "date": DAY.isoformat(), "time": "09:18:03"},                  # 合格
        {"code": "600001", "bid_volume": AUCTION_SHARES, "ask_volume": AUCTION_SHARES,
         "bid_price": 10.5, "ask_price": 10.5, "prev_close": 10.0,
         "date": "2026-09-28", "time": "15:34:59"},                     # 日期不匹配 → 无源钟
        {"code": "600002", "bid_volume": AUCTION_SHARES, "ask_volume": AUCTION_SHARES,
         "bid_price": 10.5, "ask_price": 10.5, "prev_close": 10.0,
         "date": "", "time": ""},                                       # 时钟缺失
        {"code": "600003", "bid_volume": AUCTION_SHARES, "ask_volume": AUCTION_SHARES,
         "bid_price": 10.5, "ask_price": 10.5, "prev_close": 10.0,
         "date": DAY.isoformat(), "time": "09:26:01"},                  # 终场 → 让给腾讯/东财
    ])
    async with maker() as db:
        await _seed_kline(db, ["600000", "600001", "600002", "600003"])
        result = await AuctionCollector().collect_sina_auction_evidence(
            db, DAY, now=datetime(2026, 9, 29, 9, 18, 6),
            codes=["600000", "600001", "600002", "600003"],
        )
        assert result["status"] == "ok"
        assert result["accepted"] == 1 and result["written"] == 1
        assert result["missing_source_clock"] == 2
        rows = (await db.scalars(select(AuctionData))).all()
        assert [r.code for r in rows] == ["600000"]

    row = rows[0]
    assert row.source == "sina"
    assert row.source_version == SINA_AUCTION_SOURCE_VERSION
    assert row.price_basis == "indicative_match"
    assert row.volume_basis == "indicative_matched"
    assert row.volume_unit == "share" and row.amount_unit == "CNY"
    assert row.source_quote_at == datetime(2026, 9, 29, 9, 18, 3)
    assert row.auction_time == "09:18:06"
    # 量比由既有公式换算：1,200,000/12,000,000×20 = 2.0
    assert row.volume_ratio == 2.0
    assert auction_evidence_status(row, decision_at=datetime(2026, 9, 29, 9, 18, 6)) == "ok"


@pytest.mark.asyncio
async def test_no_kline_history_stays_blocked_instead_of_faking_ratio(maker, monkeypatch):
    """没有 5 日均量时量比保持 0 → 契约按 incomplete_values 拒收，不许补齐。"""
    _patch(monkeypatch, [{"code": "600000", "bid_volume": AUCTION_SHARES,
                          "ask_volume": AUCTION_SHARES, "bid_price": 10.5,
                          "ask_price": 10.5, "prev_close": 10.0}])
    async with maker() as db:
        result = await AuctionCollector().collect_sina_auction_evidence(
            db, DAY, now=datetime(2026, 9, 29, 9, 18, 6), codes=["600000"],
        )
        assert result["status"] == "no_verified_rows"
        assert result["rejected"].get("incomplete_values") == 1
        assert (await db.scalars(select(AuctionData))).all() == []


@pytest.mark.asyncio
async def test_zero_volume_or_zero_price_rows_are_rejected_not_faked(maker, monkeypatch):
    """竞价期虚拟匹配量为 0 时**不得**当作正量帧写入 —— 取到 0 就是 0。"""
    _patch(monkeypatch, [
        {"code": "600000", "bid_volume": 0, "ask_volume": 0,
         "bid_price": 10.5, "ask_price": 10.5, "prev_close": 10.0},
        {"code": "600001", "bid_volume": AUCTION_SHARES, "ask_volume": AUCTION_SHARES,
         "bid_price": 0, "ask_price": 0, "prev_close": 10.0},
    ])
    async with maker() as db:
        result = await AuctionCollector().collect_sina_auction_evidence(
            db, DAY, now=datetime(2026, 9, 29, 9, 18, 6),
            codes=["600000", "600001"],
        )
        assert result["status"] == "no_verified_rows"
        assert result["written"] == 0
        # 档位缺失的行在构造候选帧前就被拒，原因以计数留痕（不再依赖契约分类）。
        assert result["level1_incomplete"] == 2
        assert (await db.scalars(select(AuctionData))).all() == []


@pytest.mark.asyncio
async def test_repeated_sampling_accumulates_distinct_source_clocks(maker, monkeypatch):
    """D2 的不可撤单段要 >=2 个不同源时刻的 ok 帧 —— 逐轮采样必须累积成多帧。"""
    async def run_at(quote_time: str, observed: datetime):
        _patch(monkeypatch, [{
            "code": "600000", "bid_volume": AUCTION_SHARES, "ask_volume": AUCTION_SHARES,
            "bid_price": 10.5, "ask_price": 10.5, "prev_close": 10.0,
            "date": DAY.isoformat(), "time": quote_time,
        }], received_at=observed)
        async with maker() as db:
            await _seed_kline(db, ["600000"])
            return await AuctionCollector().collect_sina_auction_evidence(
                db, DAY, now=observed, codes=["600000"],
            )

    first = await run_at("09:20:32", datetime(2026, 9, 29, 9, 20, 35))
    second = await run_at("09:22:31", datetime(2026, 9, 29, 9, 22, 34))
    assert first["status"] == second["status"] == "ok"
    async with maker() as db:
        rows = (await db.scalars(select(AuctionData))).all()
        assert len(rows) == 2
        assert len({r.source_quote_at for r in rows}) == 2
        assert {r.auction_time for r in rows} == {"09:20:35", "09:22:34"}


@pytest.mark.asyncio
async def test_identical_source_clock_is_not_a_second_frame(maker, monkeypatch):
    """同一供应商秒钟的重复响应不是第二帧（唯一键 + 帧身份键必须拦住）。"""
    async def run_at(observed: datetime):
        _patch(monkeypatch, [{
            "code": "600000", "bid_volume": AUCTION_SHARES, "ask_volume": AUCTION_SHARES,
            "bid_price": 10.5, "ask_price": 10.5, "prev_close": 10.0,
            "date": DAY.isoformat(), "time": "09:18:03",
        }], received_at=observed)
        async with maker() as db:
            await _seed_kline(db, ["600000"])
            return await AuctionCollector().collect_sina_auction_evidence(
                db, DAY, now=observed, codes=["600000"],
            )

    await run_at(datetime(2026, 9, 29, 9, 18, 6))
    await run_at(datetime(2026, 9, 29, 9, 18, 20))
    async with maker() as db:
        rows = (await db.scalars(select(AuctionData))).all()
        assert len(rows) == 1
        assert len({r.source_frame_key for r in rows}) == 1


@pytest.mark.asyncio
async def test_frames_fill_the_d2_early_and_cancel_buckets(maker, monkeypatch):
    """决定性验证：新帧必须落进 D2 判据的 09:20 前段与 09:20–09:25 段。

    判据取自 `strategy_iteration_shadow` 的现网分桶：
    `row_time = source_clock.time() if status == "ok" else auction_time`，
    早段 <09:20、不可撤单段 09:20–09:25、终场 09:25–09:30；
    且不可撤单段要求 `distinct_source_quote_at >=
    settings.PAPER_STRATEGY_D_AUCTION_MIN_CANCEL_PHASE_SAMPLES`。
    """
    from app.config.settings import settings
    from app.data.fund_flow_clock import local_clock

    async def run_at(quote_time: str, observed: datetime):
        _patch(monkeypatch, [{
            "code": "600000", "bid_volume": AUCTION_SHARES, "ask_volume": AUCTION_SHARES,
            "bid_price": 10.5, "ask_price": 10.5, "prev_close": 10.0,
            "date": DAY.isoformat(), "time": quote_time,
        }], received_at=observed)
        async with maker() as db:
            await _seed_kline(db, ["600000"])
            return await AuctionCollector().collect_sina_auction_evidence(
                db, DAY, now=observed, codes=["600000"],
            )

    observed = datetime(2026, 9, 29, 9, 24, 40)
    await run_at("09:17:02", datetime(2026, 9, 29, 9, 17, 5))
    await run_at("09:21:11", datetime(2026, 9, 29, 9, 21, 14))
    await run_at("09:23:32", datetime(2026, 9, 29, 9, 23, 35))

    async with maker() as db:
        rows = list((await db.scalars(select(AuctionData))).all())
    assert len(rows) == 3

    early, cancel_phase, final = [], [], []
    for row in rows:
        status = auction_evidence_status(row, decision_at=observed)
        source_clock = local_clock(row.source_quote_at)
        assert status == "ok", (row.source_quote_at, status)
        row_time = source_clock.time()
        if row_time < time(9, 20):
            early.append(row)
        elif time(9, 20) <= row_time < time(9, 25):
            cancel_phase.append(row)
        elif time(9, 25) <= row_time < time(9, 30):
            final.append(row)

    assert len(early) == 1, "09:20 前必须有可认证帧（否则 verified_early 恒为空）"
    assert len(cancel_phase) == 2, "不可撤单段要有两个可认证帧"
    assert len({local_clock(row.source_quote_at) for row in cancel_phase}) >= max(
        int(settings.PAPER_STRATEGY_D_AUCTION_MIN_CANCEL_PHASE_SAMPLES), 1
    )
    assert final == [], ">=09:25 的终场帧必须留给腾讯/东财来源"


@pytest.mark.asyncio
async def test_network_failure_writes_nothing_and_reports_why(maker, monkeypatch):
    _patch(monkeypatch, [], raises=True)
    async with maker() as db:
        result = await AuctionCollector().collect_sina_auction_evidence(
            db, DAY, now=datetime(2026, 9, 29, 9, 18, 6), codes=["600000"],
        )
        assert result["status"] == "no_verified_rows"
        assert result["fetch_diagnostics"]["failed_batches"] >= 1
        assert (await db.scalars(select(AuctionData))).all() == []


@pytest.mark.asyncio
async def test_universe_is_tradeable_non_st_non_suspended(maker, monkeypatch):
    captured = {}

    class _Resp:
        text = _sina_payload([{
            "code": "600000", "bid_volume": AUCTION_SHARES, "ask_volume": AUCTION_SHARES,
            "bid_price": 10.5, "ask_price": 10.5, "prev_close": 10.0,
        }])
        status_code = 200

        def raise_for_status(self):
            return None

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, params=None, headers=None):
            captured["url"] = url
            return _Resp()

    monkeypatch.setattr("httpx.AsyncClient", _Client)
    async with maker() as db:
        db.add_all([
            StockTag(code="600000", name="主板股", board_type="main_sh", board_tag="tradeable"),
            StockTag(code="300750", name="创业板", board_type="gem", board_tag="observe_only"),
            StockTag(code="600001", name="ST股", board_type="main_sh", board_tag="blocked", is_st=True),
            StockTag(code="600002", name="停牌股", board_type="main_sh", board_tag="suspended",
                     is_suspended=True),
        ])
        await db.commit()
        await AuctionCollector().collect_sina_auction_evidence(
            db, DAY, now=datetime(2026, 9, 29, 9, 18, 6),
        )
    assert "sh600000" in captured["url"]
    assert "sz300750" not in captured["url"]
    assert "sh600001" not in captured["url"]
    assert "sh600002" not in captured["url"]


# ---------------------------------------------------------------------------
# 遮蔽竞态：无源时钟的观察行不得顶掉本日已认证的证据帧
# ---------------------------------------------------------------------------
# `get_snapshot_health` 按每只代码 `MAX(auction_time)` 取"最新帧"。
# 观察行的 auction_time 是**本机接收时刻**，完全可能晚于证据帧的接收时刻；
# 一旦晚，`multi_frame_complete_count` 与 `verified_timely_snapshot_ratio`
# 就一起回落成 0。观察行本来就不进任何判定，删掉不损失证据。

def test_observed_spot_rows_yield_to_verified_frames(maker_unused=None):
    """源码级锁定：`collect_auction_data` 必须在落库前让出已认证代码。"""
    import inspect

    body = inspect.getsource(AuctionCollector.collect_auction_data)
    assert "_verified_auction_codes" in body
    assert "遮蔽" in body


@pytest.mark.asyncio
async def test_verified_codes_are_exactly_the_contract_ok_rows(maker):
    async with maker() as db:
        await _seed_kline(db, ["600000", "600001"])
        db.add_all([
            AuctionData(code="600000", trade_date=DAY, auction_time="09:18:00",
                        auction_price=10.5, auction_volume=AUCTION_SHARES,
                        auction_amount=12_600_000.0, prev_close=10.0, volume_ratio=2.0,
                        source="sina", source_version=SINA_AUCTION_SOURCE_VERSION,
                        source_quote_at=datetime(2026, 9, 29, 9, 17, 58),
                        received_at=datetime(2026, 9, 29, 9, 18, 0),
                        observed_at=datetime(2026, 9, 29, 9, 18, 0),
                        price_basis="indicative_match", volume_basis="indicative_matched",
                        volume_unit="share", amount_unit="CNY"),
            # 无源时钟的观察行：绝不能被算作"已认证"
            AuctionData(code="600001", trade_date=DAY, auction_time="09:18:05",
                        auction_price=10.5, auction_volume=0, auction_amount=0.0,
                        prev_close=10.0, volume_ratio=0.0, source="sina",
                        source_version="akshare_spot_open_v1_unverified",
                        source_quote_at=None,
                        received_at=datetime(2026, 9, 29, 9, 18, 5),
                        observed_at=datetime(2026, 9, 29, 9, 18, 5),
                        price_basis="spot_open_unverified",
                        volume_basis="intraday_cumulative",
                        volume_unit="share", amount_unit="CNY"),
        ])
        await db.commit()
        codes = await AuctionCollector()._verified_auction_codes(
            db, DAY, decision_at=datetime(2026, 9, 29, 9, 18, 6),
        )
    assert codes == {"600000"}


# ---------------------------------------------------------------------------
# 2026-09-29 冻结样本：腾讯/新浪早中段均读档位，不读累计成交量
# ---------------------------------------------------------------------------
# 实测（09:14:30–09:27:00，10 只样本、900 个样本，见
# outputs/auction_source_capability_20260929/samples.jsonl）：
#   * 新浪最新价/成交量为0不代表无虚拟匹配；其档位量单位是股。
#   * 腾讯买一量 == 卖一量（900/900 = 100%）、为正 828/900、且随时间推进
#     （600000：139→174→226→291→366→405 手）；field[30] 每约 3 秒推进；
#   * 同窗口腾讯 field[6]成交量/field[37]成交额/field[49]量比恒为 0。
# 交易所规格（上交所行情规格 3.10 / 深交所 STEP）：集合竞价期间买卖价均为虚拟
# 开盘参考价，「申买量一」「申卖量一」为虚拟匹配量。

def _tencent_early_record(code="600000", *, clock="20260929091803", **over):
    record = {
        "code": code, "name": "测试股",
        "price": 10.5, "prev_close": 10.0, "open": 0.0,
        "volume": 0, "amount": 0.0, "volume_ratio": 0.0,
        "bid1_price": 10.5, "bid1_volume": 1_200,
        "ask1_price": 10.5, "ask1_volume": 1_200,
        "source_quote_at": datetime.strptime(clock, "%Y%m%d%H%M%S"),
        "received_at": datetime(2026, 9, 29, 9, 18, 4),
        "observed_at": datetime(2026, 9, 29, 9, 18, 4),
    }
    record.update(over)
    return record


def _patch_tencent(monkeypatch, records, *, received_at=None):
    """替身必须带 `observed_at`：采集器用它当接收时钟，缺失会被判 invalid_clock。"""

    async def fake(self, codes):
        out = []
        for record in records:
            item = dict(record)
            if received_at is not None:
                # 契约要求 source_quote_at <= received_at <= observed_at；
                # 替身统一用调用时刻，避免"时钟倒挂"造成的假失败。
                item["received_at"] = received_at
                item["observed_at"] = received_at
            out.append(item)
        return out

    from app.data.sources.tencent_source import TencentSource

    monkeypatch.setattr(TencentSource, "collect_spot_batch", fake)


@pytest.mark.asyncio
async def test_tencent_early_writes_virtual_match_from_level1(maker, monkeypatch):
    from app.strategy.auction import TENCENT_AUCTION_EARLY_SOURCE_VERSION

    moment = datetime(2026, 9, 29, 9, 18, 6)
    _patch_tencent(monkeypatch, [_tencent_early_record()], received_at=moment)
    async with maker() as db:
        await _seed_kline(db, ["600000"])
        result = await AuctionCollector().collect_tencent_auction_early_evidence(
            db, DAY, now=moment, codes=["600000"],
        )
        assert result["status"] == "ok", result
        row = (await db.scalars(select(AuctionData))).one()
    assert row.source == "tencent"
    assert row.source_version == TENCENT_AUCTION_EARLY_SOURCE_VERSION
    assert row.price_basis == "indicative_match"
    assert row.volume_basis == "indicative_matched"
    assert row.volume_unit == "lot100"
    assert row.auction_price == 10.5, "虚拟参考价取买一/卖一价"
    assert row.auction_volume == 1_200, "虚拟匹配量取买一/卖一量的最小正值（手）"
    assert row.auction_amount == 1_260_000.0, "成交额按 参考价×手数×100 换算"
    assert row.source_quote_at == datetime(2026, 9, 29, 9, 18, 3)
    assert auction_evidence_status(row, decision_at=datetime(2026, 9, 29, 9, 18, 6)) == "ok"
    assert row.volume_ratio == 0.2, "量比=匹配量(股)/5日均量×20 = 1,200×100/12,000,000×20"


@pytest.mark.asyncio
async def test_tencent_early_rejects_inconsistent_sides(maker, monkeypatch):
    """取最小值不是语义认证：不重合的档位不得写成虚拟匹配证据。"""
    _patch_tencent(monkeypatch, [
        _tencent_early_record("600000", bid1_volume=1_000, ask1_volume=1_400,
                              bid1_price=10.5, ask1_price=10.4),
    ], received_at=datetime(2026, 9, 29, 9, 18, 6))
    async with maker() as db:
        await _seed_kline(db, ["600000"])
        result = await AuctionCollector().collect_tencent_auction_early_evidence(
            db, DAY, now=datetime(2026, 9, 29, 9, 18, 6), codes=["600000"],
        )
        assert result["status"] == "no_verified_rows"
        assert result["level1_unequal_or_missing"] == 1, "按拒收行数留痕，不按字段重复计数"
        assert (await db.scalars(select(AuctionData))).all() == []


@pytest.mark.asyncio
async def test_tencent_early_rejects_zero_level1_and_terminal_clock(maker, monkeypatch):
    _patch_tencent(monkeypatch, [
        _tencent_early_record("600000", bid1_volume=0, ask1_volume=0),          # 无虚拟匹配量
        _tencent_early_record("600001", clock="20260929092505"),                # 终场 → 让给终场路径
        _tencent_early_record("600002", bid1_price=0, ask1_price=0),            # 无虚拟参考价
    ], received_at=datetime(2026, 9, 29, 9, 18, 6))
    async with maker() as db:
        await _seed_kline(db, ["600000", "600001", "600002"])
        result = await AuctionCollector().collect_tencent_auction_early_evidence(
            db, DAY, now=datetime(2026, 9, 29, 9, 18, 6),
            codes=["600000", "600001", "600002"],
        )
        assert result["status"] == "no_verified_rows", result
        assert result["level1_incomplete"] == 2
        assert (await db.scalars(select(AuctionData))).all() == []


@pytest.mark.asyncio
async def test_tencent_early_outside_window_writes_nothing(maker, monkeypatch):
    _patch_tencent(monkeypatch, [_tencent_early_record()])
    async with maker() as db:
        for moment in (datetime(2026, 9, 29, 9, 14, 59),
                       datetime(2026, 9, 29, 9, 25, 0),
                       datetime(2026, 9, 29, 10, 0, 0)):
            result = await AuctionCollector().collect_tencent_auction_early_evidence(
                db, DAY, now=moment, codes=["600000"],
            )
            assert result["status"] == "outside_evidence_window"
        assert (await db.scalars(select(AuctionData))).all() == []


@pytest.mark.asyncio
async def test_tencent_early_frames_fill_d2_buckets(maker, monkeypatch):
    """腾讯早中段帧必须落进 D2 的 09:20 前段与不可撤单段。"""
    from app.config.settings import settings
    from app.data.fund_flow_clock import local_clock

    async def run_at(clock: str, observed: datetime):
        _patch_tencent(monkeypatch, [_tencent_early_record(clock=clock)], received_at=observed)
        async with maker() as db:
            await _seed_kline(db, ["600000"])
            return await AuctionCollector().collect_tencent_auction_early_evidence(
                db, DAY, now=observed, codes=["600000"],
            )

    observed = datetime(2026, 9, 29, 9, 24, 40)
    await run_at("20260929091610", datetime(2026, 9, 29, 9, 16, 12))
    await run_at("20260929092100", datetime(2026, 9, 29, 9, 21, 2))
    await run_at("20260929092330", datetime(2026, 9, 29, 9, 23, 32))

    async with maker() as db:
        rows = list((await db.scalars(select(AuctionData))).all())
    times = sorted(local_clock(r.source_quote_at).time() for r in rows)
    assert len(rows) == 3
    assert sum(1 for t in times if t < time(9, 20)) == 1
    assert sum(1 for t in times if time(9, 20) <= t < time(9, 25)) == 2
    assert len({local_clock(r.source_quote_at) for r in rows}) >= max(
        int(settings.PAPER_STRATEGY_D_AUCTION_MIN_CANCEL_PHASE_SAMPLES), 1
    )


def test_early_job_samples_tencent_first():
    """现有早中段任务依次调用两源的档位采集器，不宣称并行或来源替代时间帧。"""
    import inspect

    from app.data.scheduler import DataScheduler

    body = inspect.getsource(DataScheduler._auction_collect_sina_evidence)
    assert "collect_tencent_auction_early_evidence" in body
    assert body.index("collect_tencent_auction_early_evidence") < body.index(
        "collect_sina_auction_evidence"
    )


def _patch_early_rows(monkeypatch, source, rows):
    if source == "tencent":
        _patch_tencent(monkeypatch, rows)
        return "collect_tencent_auction_early_evidence"

    async def fetch(self, codes, *, diagnostics=None):
        return {r["code"]: {
            "code": r["code"], "date": r["source_quote_at"].strftime("%Y-%m-%d"),
            "time": r["source_quote_at"].strftime("%H:%M:%S"),
            "bid_price": r["bid1_price"], "ask_price": r["ask1_price"],
            "bid_volume": r["bid1_volume"] * 100, "ask_volume": r["ask1_volume"] * 100,
            "prev_close": r["prev_close"],
            "received_at": r.get("received_at"), "observed_at": r.get("observed_at"),
        } for r in rows}

    monkeypatch.setattr(AuctionCollector, "_fetch_sina_auction_quotes", fetch)
    return "collect_sina_auction_evidence"


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["tencent", "sina"])
@pytest.mark.parametrize("change", [
    {"ask1_price": 10.4}, {"ask1_volume": 1199},
    {"ask1_price": float("nan")}, {"bid1_volume": float("inf")},
])
async def test_early_invalid_level1_never_becomes_verified(maker, monkeypatch, source, change):
    moment = datetime(2026, 9, 29, 9, 18, 6)
    method = _patch_early_rows(monkeypatch, source, [_tencent_early_record(**change)])
    async with maker() as db:
        await _seed_kline(db, ["600000"])
        result = await getattr(AuctionCollector(), method)(db, DAY, now=moment, codes=["600000"])
        assert result["written"] == 0
        assert (await db.scalars(select(AuctionData))).all() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["tencent", "sina"])
@pytest.mark.parametrize("missing", ["received_at", "observed_at"])
async def test_early_missing_response_clock_cannot_be_replaced_by_now(maker, monkeypatch, source, missing):
    method = _patch_early_rows(monkeypatch, source, [_tencent_early_record(**{missing: None})])
    async with maker() as db:
        await _seed_kline(db, ["600000"])
        result = await getattr(AuctionCollector(), method)(
            db, DAY, now=datetime(2026, 9, 29, 9, 18, 6), codes=["600000"])
        assert result["written"] == 0
        assert result["rejected"] == {"unknown": 1}


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["tencent", "sina"])
async def test_early_keeps_each_response_clock_when_tail_finishes_late(maker, monkeypatch, source):
    early = datetime(2026, 9, 29, 9, 24, 59)
    late = datetime(2026, 9, 29, 9, 25, 31)
    method = _patch_early_rows(monkeypatch, source, [
        _tencent_early_record(clock="20260929092457", received_at=early, observed_at=early),
        _tencent_early_record("600001", clock="20260929092457", received_at=late, observed_at=late),
    ])
    # now=None exercises separate precheck/completion clocks, never a fake production time.
    import app.strategy.auction as module
    moments = iter([early, late])

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return next(moments)

    monkeypatch.setattr(module, "datetime", Clock)
    async with maker() as db:
        await _seed_kline(db, ["600000", "600001"])
        result = await getattr(AuctionCollector(), method)(db, DAY, codes=["600000", "600001"])
        assert result["written"] == 1
        assert result["rejected"] == {"invalid_clock": 1}
        row = (await db.scalars(select(AuctionData))).one()
        assert row.code == "600000"
        assert row.observed_at == row.received_at == early


@pytest.mark.asyncio
async def test_cross_source_same_clock_is_one_temporal_frame(maker, monkeypatch):
    moment = datetime(2026, 9, 29, 9, 18, 6)
    record = _tencent_early_record()
    async with maker() as db:
        await _seed_kline(db, ["600000"])
        db.add(StockTag(code="600000", name="主板股", board_type="main_sh", board_tag="tradeable"))
        await db.commit()
        for source in ("tencent", "sina"):
            method = _patch_early_rows(monkeypatch, source, [record])
            result = await getattr(AuctionCollector(), method)(db, DAY, now=moment, codes=["600000"])
            assert result["written"] == 1
        rows = (await db.scalars(select(AuctionData))).all()
        assert len(rows) == 2 and len({r.source_frame_key for r in rows}) == 2
        assert len({r.source_quote_at for r in rows}) == 1
        health = await AuctionCollector().get_snapshot_health(db, DAY)
        assert health["multi_frame_complete_count"] == 0
        # Only a genuine new provider time, not changing source/version, adds a frame.
        record = _tencent_early_record(clock="20260929091805", received_at=moment, observed_at=moment)
        method = _patch_early_rows(monkeypatch, "tencent", [record])
        await getattr(AuctionCollector(), method)(db, DAY, now=moment, codes=["600000"])
        assert (await AuctionCollector().get_snapshot_health(db, DAY))["multi_frame_complete_count"] == 1

