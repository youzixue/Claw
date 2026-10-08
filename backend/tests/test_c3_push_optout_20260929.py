"""C3 notification opt-out must not disable research or ordinary paper notices."""
import asyncio
from datetime import datetime
from unittest.mock import AsyncMock

import pytest

from app.config.settings import Settings, settings
from app.push import paper_buy_points as points


def test_c3_notifications_default_off_without_disabling_other_pushes():
    assert Settings.model_fields["C3_RESEARCH_PUSH_ENABLED"].default is False
    assert Settings.model_fields["PUSH_ENABLED"].default is True
    assert Settings.model_fields["PAPER_BUY_POINT_PUSH_ENABLED"].default is True


@pytest.mark.asyncio
async def test_disabled_c3_never_enters_database_or_transport(monkeypatch):
    monkeypatch.setattr(settings, "C3_RESEARCH_PUSH_ENABLED", False)
    monkeypatch.setattr(settings, "PUSH_ENABLED", True)
    monkeypatch.setattr(points, "_c3_lock", asyncio.Lock())
    monkeypatch.setattr(points, "_runtime", {})
    dispatch = AsyncMock(side_effect=AssertionError("disabled C3 must not enter dispatcher"))
    transport = AsyncMock(side_effect=AssertionError("disabled C3 must not send"))
    monkeypatch.setattr(points, "_dispatch_c3", dispatch)
    monkeypatch.setattr(points.push_scheduler, "push_to_channels", transport)
    def no_database():
        raise AssertionError("disabled C3 must not open a database session")
    result = await points.dispatch_c3_research(
        now=datetime(2026, 9, 29, 10, 0), session_factory=no_database,
    )
    assert result == {"status": "disabled"}
    assert points._runtime["c3_research"] == {"status": "disabled"}
    dispatch.assert_not_awaited()
    transport.assert_not_awaited()


@pytest.mark.asyncio
async def test_c3_optout_does_not_disable_ordinary_dispatch(monkeypatch):
    monkeypatch.setattr(settings, "C3_RESEARCH_PUSH_ENABLED", False)
    monkeypatch.setattr(settings, "PUSH_ENABLED", True)
    monkeypatch.setattr(settings, "PAPER_BUY_POINT_PUSH_ENABLED", True)
    monkeypatch.setattr(points, "_dispatch_lock", asyncio.Lock())
    dispatch = AsyncMock(return_value={"status": "sent", "count": 2})
    research = AsyncMock(side_effect=AssertionError("ordinary path must not call C3"))
    monkeypatch.setattr(points, "_dispatch", dispatch)
    monkeypatch.setattr(points, "dispatch_c3_research", research)
    factory = object()
    at = datetime(2026, 9, 29, 10, 0)
    result = await points.dispatch_buy_points(now=at, session_factory=factory)
    assert result == {"status": "sent", "count": 2}
    dispatch.assert_awaited_once_with(now=at, session_factory=factory)
    research.assert_not_awaited()
