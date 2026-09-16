"""Public execution fixtures must not fetch a calendar from global DB/network."""
from datetime import date
import importlib

import pytest

from app.api.v1 import paper
from test_paper_public_boundary_20260914 import public_env, paper_client, qualified_execution_risk


@pytest.mark.asyncio
@pytest.mark.parametrize("start,end,expected", [
    (date(2026, 9, 11), date(2026, 9, 14), [date(2026, 9, 11), date(2026, 9, 14)]),
    (date(2026, 9, 12), date(2026, 9, 13), []),
])
async def test_public_fixture_holding_days_use_only_its_local_calendar(
        public_env, monkeypatch, start, end, expected):
    # public_env deliberately requires real registered-risk evidence at teardown.
    # Exercise its precheck, without submitting an order or mocking that chain.
    from app.trading import service
    _, factory, at = public_env
    async with factory() as db:
        risk = await service._pre_trade_risk_check(db, service.SubmitOrderCommand(
            code="000001", side="buy", price=10, quantity=100, decision_at=at))
    assert risk["checked_rules"] == 10 and risk["evaluation_status"] == "complete"
    module = importlib.import_module("app.core.trade_calendar")
    def forbidden():
        raise AssertionError("public fixture must not open global calendar database")
    monkeypatch.setattr(module, "async_session", forbidden)
    before = dict(paper.trade_calendar._cache)
    assert await paper.trade_calendar.trade_days_between(start, end) == expected
    assert paper.trade_calendar._cache == before
    with pytest.raises(AssertionError, match="unsupported public fixture calendar year"):
        await paper.trade_calendar._ensure_loaded(2025)
