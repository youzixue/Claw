"""Targeted SQL string and same-security fixed-price structure counterexamples."""
import pytest
from sqlalchemy import insert, update
from app.models.trading import TradeOrder
from test_paper_after_hours_partial_history_20261002 import database, fragments, read, damaged
from test_paper_after_hours_partial_schema_20261002 import header, AT


@pytest.mark.asyncio
@pytest.mark.parametrize("model,field",[(TradeOrder,"strategy_version"),(TradeOrder,"decision_round_id")])
async def test_nul_cannot_bypass_declared_selected_leaf_length(database, model, field):
    maker, engine = database
    await fragments(engine,1)
    async with damaged(engine) as conn:
        await conn.execute(update(model).values(**{field:"\0"+"x"*getattr(model,field).type.length}))
    async with maker() as db:
        with pytest.raises(ValueError, match="resource_book_text_byte_budget_or_invalid_leaf"):
            await read(db)


@pytest.mark.asyncio
async def test_exact_declared_unicode_leaf_remains_valid(database):
    maker, engine = database
    await fragments(engine,1)
    async with damaged(engine) as conn:
        await conn.execute(update(TradeOrder).values(strategy_version="汉"*64))
    async with maker() as db:
        assert (await read(db))["receipt_count"] == 1


@pytest.mark.asyncio
async def test_all_originals_same_security_day_need_same_fixed_price(database):
    maker, engine = database
    async with engine.begin() as conn:
        await conn.execute(insert(TradeOrder).values(id=2, order_id="other", broker="paper",
            account_id="default", order_type="after_hours_fixed", code="600000", side="buy",
            price=10.5, quantity=100, filled_quantity=0, status="submitted", trade_date=AT.date(),
            created_at=AT, decision_at=AT))
        await conn.run_sync(lambda c: header(c,1))
        await conn.run_sync(lambda c: header(c,2,order_id="other",original_quantity=100,fragment_index=1,
            book_changes={"trade":{"price":9},"fill":{"price":9}},payload_changes={"fixed_price":9}))
    async with maker() as db:
        with pytest.raises(ValueError,match="fixed_price"):
            await read(db)
