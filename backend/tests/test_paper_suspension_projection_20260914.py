"""A currently active suspended blacklist is a halt, including sell exits."""
import pytest
from sqlalchemy import update, select

from app.api.v1 import paper
from app.core.stock_tagger import stock_tagger
from app.models.stock import StockBlacklist
from app.models.trading import TradeOrder
from test_quote_round_execution import quote_execution_env
from test_paper_locked_risk_20260914 import (
    real_risk_calendar, qualified, MutationBeforeAcquisition, PATHS, AT, fill_count,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", PATHS)
async def test_blacklist_halt_published_during_wait_blocks_both_sides(
    quote_execution_env, monkeypatch, path,
):
    invoke, _, _, _ = await qualified(quote_execution_env, monkeypatch, path)
    async def halt():
        async with quote_execution_env() as db:
            await db.execute(update(StockBlacklist).values(reason="suspended", end_date=None))
            await db.commit()
    monkeypatch.setattr(paper, "_TRADE_LOCK", MutationBeforeAcquisition(halt))
    result = await invoke(0)
    assert fill_count(result) == 0
    async with quote_execution_env() as db:
        order = (await db.scalars(select(TradeOrder))).one()
        assert order.status == "risk_blocked"


