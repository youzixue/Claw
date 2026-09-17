"""未登记号段不得中断整批标记（2026-09-17 盘后实测缺陷）。

实测：当日 `个股映射写入: 81187条` 成功，紧随其后的 `股票标记写入`
**整行缺失**（对照 8/17、8/18、8/24 均写入 3,400~4,962 只）。
原因是 `689009`（科创板 CDR）与 `302132`（深主板新号段）当时未登记，
而 `tag_stock` 对 unknown 直接抛错、`batch_tag` 逐个调用 → 整批丢失。

本文件锁定：① 两个号段已登记且与 `price_limit_rules` 一致；
② 出现新的未知号段时只跳过该项，其余照常标记。
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.price_limit_rules import board_for_code
from app.core.stock_tagger import (
    BOARD_TYPE_MAIN_SZ,
    BOARD_TYPE_STAR,
    stock_tagger,
)
from app.db.session import Base
from app.models.stock import StockTag

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def session(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as db:
        yield db
    await engine.dispose()


@pytest.mark.parametrize("code,expected", [
    ("689009", BOARD_TYPE_STAR),       # 九号公司-WD，科创板 CDR
    ("302132", BOARD_TYPE_MAIN_SZ),    # 中航成飞，深主板新号段
])
def test_new_code_ranges_are_registered(code, expected):
    assert stock_tagger.get_board_type(code) == expected


@pytest.mark.parametrize("code", ["689009", "302132", "688009", "300123", "920045", "600000"])
def test_code_ranges_agree_with_price_limit_rules(code):
    """两处号段判定必须一致 —— 不一致就是下一次事故的种子。"""
    tagger_board = stock_tagger.get_board_type(code)
    rules_board = board_for_code(code)
    # price_limit_rules 用 main 统称沪深主板
    if rules_board == "main":
        assert tagger_board in {"main_sh", "main_sz", "sme"}, (code, tagger_board)
    else:
        assert tagger_board == rules_board, (code, tagger_board, rules_board)


async def test_unknown_range_does_not_abort_the_batch(session):
    """核心保证：一个未登记号段只跳过自己，整批其余照常标记。"""
    stocks = [
        {"code": "600000", "name": "浦发银行"},
        {"code": "000001", "name": "平安银行"},
        {"code": "777777", "name": "未登记号段"},   # 故意未知
        {"code": "300750", "name": "宁德时代"},
    ]
    tagged = await stock_tagger.batch_tag(session, stocks)
    await session.commit()

    assert tagged == 3, "未知号段应被跳过而不是中断整批"
    codes = {row.code for row in (await session.scalars(select(StockTag))).all()}
    assert {"600000", "000001", "300750"} <= codes
    assert "777777" not in codes


async def test_batch_tag_still_raises_for_direct_single_call(session):
    """单只 `tag_stock` 的严格校验保留 —— 只放宽批量路径的失败隔离。"""
    with pytest.raises(ValueError, match="invalid_or_unknown_stock_code"):
        await stock_tagger.tag_stock(session, code="777777", name="未登记")


async def test_skipped_codes_become_an_auditable_health_signal(session):
    """跳过不能只写日志：必须在 data_source_health 留下可查、会升级的记录。

    2026-09-17 的 `invalid_or_unknown_stock_code` 只在日志里出现，
    数据源健康表看不到，汇总告警不亮，只能人工翻日志才发现整批标记丢失。
    """
    from sqlalchemy import select

    from app.models.risk import DataSourceHealth

    stocks = [
        {"code": "600000", "name": "浦发银行"},
        {"code": "777777", "name": "未登记A"},
        {"code": "888888", "name": "未登记B"},
    ]
    assert await stock_tagger.batch_tag(session, stocks) == 1

    rows = (await session.scalars(
        select(DataSourceHealth)
        .where(DataSourceHealth.source == "stock_tagger")
        .order_by(DataSourceHealth.id.desc())
    )).all()
    assert rows, "跳过未登记号段必须落健康记录"
    latest = rows[0]
    assert latest.api_name == "unregistered_code_segment"
    assert latest.status == "degraded", "首次出现按 degraded，连续出现才升 down"
    assert latest.fail_streak == 1
    assert "777777" in latest.error_msg and "888888" in latest.error_msg
    assert "'777'" in latest.error_msg and "'888'" in latest.error_msg

    # 连续出现要升级为 down，否则长期未登记号段会被当成一次性抖动
    assert await stock_tagger.batch_tag(session, stocks) == 1
    assert await stock_tagger.batch_tag(session, stocks) == 1
    latest = (await session.scalars(
        select(DataSourceHealth)
        .where(DataSourceHealth.source == "stock_tagger")
        .order_by(DataSourceHealth.id.desc())
        .limit(1)
    )).one()
    assert latest.fail_streak == 3 and latest.status == "down"


async def test_no_health_signal_when_nothing_was_skipped(session):
    """全部号段已登记时不得产生噪声记录。"""
    from sqlalchemy import select

    from app.models.risk import DataSourceHealth

    await stock_tagger.batch_tag(session, [{"code": "600000", "name": "浦发银行"}])
    assert (await session.scalars(
        select(DataSourceHealth).where(DataSourceHealth.source == "stock_tagger")
    )).all() == []


def test_every_live_code_prefix_is_registered():
    """回归护栏：全市场现存代码必须 100% 能判定板块。

    用真实库快照，未登记号段一出现这里就红，而不是等到整批标记静默丢失。
    """
    import sqlite3
    from pathlib import Path

    db = Path(__file__).resolve().parents[1] / "claw.db"
    if not db.exists():
        pytest.skip("本地开发库不存在，跳过真实快照护栏")
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = conn.execute("SELECT DISTINCT code FROM stock_tags").fetchall()
    finally:
        conn.close()
    unknown = sorted(code for (code,) in rows if stock_tagger.get_board_type(code) == "unknown")
    assert unknown == [], f"存在未登记号段: {unknown[:20]}"
