"""Extra dormant-kernel negatives, still fabricated future scope / temporary rows."""
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import select, func
from sqlalchemy.sql.dml import Update

from app.api.v1 import paper
from app.models.paper import PaperAccount
from app.models.trading import TradeFill, PaperAfterHoursResourceScope as State, PaperAfterHoursResourceReceipt as Receipt
from app.trading import paper_after_hours_resources as resources, paper_after_hours_allocation as allocation
from test_paper_after_hours_allocation_20261002 import SOURCE, audited_fixture
from test_paper_after_hours_partial_contract_20261002 import original, freeze
from test_paper_after_hours_partial_allocation_20261002 import capacity
from test_paper_after_hours_partial_book_20261002 import book
from test_paper_after_hours_partial_fees_20261002 import fee_model
from test_paper_after_hours_partial_resources_20261002 import database, stage, persist, future_scope


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["source_version", "session_id"])
async def test_new_original_cannot_reset_existing_security_day_root(database, audited_fixture, fee_model, monkeypatch, change):
    maker, _ = database
    async with maker() as db, db.begin():
        await persist(db, book(freeze()))
    payload = capacity(150)
    if change == "source_version":
        payload["source_version"] = "other.v1"
        monkeypatch.setattr(allocation, "_PROVIDERS", {
            SOURCE: allocation._Provider("other.v1", "order_level", allocation.METHOD)})
    else:
        payload["session_id"] = "other.session"
    item = book(freeze(payload, original("new", sequence=2)), sequence=2)
    async with maker() as db:
        with pytest.raises(HTTPException, match="source_version_or_session_changed"):
            async with db.begin():
                await persist(db, item)
    async with maker() as db:
        assert await db.scalar(select(func.count()).select_from(State)) == 1
        assert await db.scalar(select(func.count()).select_from(Receipt)) == 1


@pytest.mark.asyncio
async def test_cas_failure_consumes_scope_and_caller_rolls_back_everything(database, audited_fixture, fee_model, monkeypatch):
    maker, _ = database
    item = book(freeze())
    async with maker() as db:
        execute = db.execute
        async def conflict(statement, *args, **kwargs):
            if isinstance(statement, Update) and statement.table.name == State.__tablename__:
                return SimpleNamespace(rowcount=0)
            return await execute(statement, *args, **kwargs)
        monkeypatch.setattr(db, "execute", conflict)
        with pytest.raises(HTTPException, match="cas_conflict"):
            async with db.begin():
                order, fill, trade, contract, timing, req = await stage(db, item)
                with future_scope(db, req, item[0], contract, timing) as scope:
                    kwargs = dict(candidate=item[0], order_id=order.order_id, fill_id=fill.fill_id,
                                  feed=json.loads(item[0].feed_json))
                    try:
                        await resources._persist_partial_fill_resources(db, **kwargs)
                    except HTTPException:
                        assert scope.resource_consumption_used is True
                        with pytest.raises(HTTPException) as second:
                            await resources._persist_partial_fill_resources(db, **kwargs)
                        assert second.value.status_code == 403
                        raise
    async with maker() as db:
        assert await db.scalar(select(func.count()).select_from(State)) == 0
        assert await db.scalar(select(func.count()).select_from(TradeFill)) == 0
        assert (await db.get(PaperAccount, 7)).current_capital == 50000


@pytest.mark.asyncio
async def test_total_budget_exact_and_one_byte_short_includes_scope_initialization(database, audited_fixture, fee_model, monkeypatch):
    maker, _ = database
    item = book(freeze())
    async with maker() as db, db.begin():
        out = await persist(db, item)
        budget = out["read_text_bytes"]
        assert 0 < budget < resources.MAX_READ_BYTES
        await db.rollback()
    monkeypatch.setattr(resources, "MAX_READ_BYTES", budget)
    async with maker() as db, db.begin():
        assert (await persist(db, item))["read_text_bytes"] == budget
        await db.rollback()
    monkeypatch.setattr(resources, "MAX_READ_BYTES", budget-1)
    async with maker() as db:
        with pytest.raises(HTTPException, match="byte_budget"):
            async with db.begin():
                await persist(db, item)
