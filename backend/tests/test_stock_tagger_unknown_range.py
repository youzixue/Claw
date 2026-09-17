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
