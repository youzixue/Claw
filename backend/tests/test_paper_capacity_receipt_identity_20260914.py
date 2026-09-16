"""Independent review regression: do not filter away the identity being verified."""
import pytest
from sqlalchemy import select

from app.models.trading import TradeFill
from test_quote_round_execution import quote_execution_env
from test_paper_atomic_execution_20260914 import isolated_transaction_policy_and_calendar, snapshot
from test_paper_account_round_capacity_20260914 import prepare, fill_count, per_test_lock


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [
    ("fill_round_id", None), ("fill_round_id", "wrong-round"),
    ("code", "600002"), ("side", "sell"), ("broker", "wrong-broker"),
    ("order_id", "missing-order"),
])
async def test_mutable_receipt_identity_cannot_hide_original_consumption(
    quote_execution_env, monkeypatch, field, value,
):
    factory = quote_execution_env
    invoke, _, _ = await prepare(factory, monkeypatch, ["immediate-buy"]*2)
    assert fill_count(await invoke(0)) == 1
    async with factory() as db:
        fill = (await db.scalars(select(TradeFill))).one()
        setattr(fill, field, value)
        await db.commit()  # Only a temporary fixture's historical row is corrupted.
    before = await snapshot(factory)
    result = await invoke(1)
    assert fill_count(result) == 0
    assert "容量" in result[0]["order"]["error_message"]
    assert await snapshot(factory) == before
