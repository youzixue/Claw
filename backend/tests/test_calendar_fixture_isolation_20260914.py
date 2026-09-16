"""Regression for nested class/instance monkeypatch restoration; temporary DBs only."""
import pytest

from app.api.v1 import paper
from app.core.trade_calendar import TradeCalendar
from test_strategy_iteration_challenger import challenger_env
from test_paper_nav_reporting import db as nav_environment
from test_paper_entry_fee_allocation_20260914 import fee_calendar


def test_async_sqlalchemy_dependency_is_explicit_on_arm64():
    from pathlib import Path
    from packaging.requirements import Requirement
    requirements = Path(__file__).resolve().parents[1] / "requirements.txt"
    items = [Requirement(line) for line in requirements.read_text().splitlines()
             if line.strip() and not line.lstrip().startswith("#")]
    sqlalchemy = next(item for item in items if item.name.lower() == "sqlalchemy")
    assert str(sqlalchemy.specifier) == "==2.0.35"
    assert "asyncio" in sqlalchemy.extras


@pytest.mark.asyncio
@pytest.mark.parametrize("which", ["challenger", "reporting", "fee"])
async def test_calendar_fixture_does_not_leave_an_instance_bound_class_mock(tmp_path, which):
    calendar = paper.trade_calendar
    names = ("_ensure_loaded", "_sync_from_source")
    original = {name: calendar.__dict__[name] for name in names if name in calendar.__dict__}
    outer, inner = pytest.MonkeyPatch(), pytest.MonkeyPatch()
    async def outer_loaded(self, year):
        raise AssertionError("outer mock must not survive fixture teardown")
    outer.setattr(TradeCalendar, "_ensure_loaded", outer_loaded)
    outer.setattr(TradeCalendar, "_sync_from_source", outer_loaded)
    try:
        if which == "fee":
            fixture = fee_calendar.__wrapped__(inner)
            next(fixture)
            with pytest.raises(StopIteration):
                next(fixture)
        else:
            fixture = (challenger_env.__wrapped__(tmp_path, inner) if which == "challenger"
                       else nav_environment.__wrapped__(inner))
            await anext(fixture)
            with pytest.raises(StopAsyncIteration):
                await anext(fixture)
        inner.undo()
        # pytest restores an instance setattr with the saved bound class method;
        # that shadow then persists after the outer class patch is undone.
        leaked = [name for name in names
                  if (name in calendar.__dict__) != (name in original)]
        outer.undo()
        assert leaked == [], f"calendar fixture left instance method shadows: {leaked}"
        assert calendar._ensure_loaded.__func__ is TradeCalendar._ensure_loaded
    finally:
        inner.undo()
        outer.undo()
        # Even the pre-fix failing regression must not contaminate later tests.
        for name in names:
            if name in original:
                setattr(calendar, name, original[name])
            else:
                calendar.__dict__.pop(name, None)
