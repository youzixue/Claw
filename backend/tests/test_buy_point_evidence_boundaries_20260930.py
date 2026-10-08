"""Deterministic quote/sector evidence defects; no winner-fitted thresholds."""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from app.api.v1 import paper
from app.paper import strategy_iteration_shadow as shadow
from app.models.stock import StockSectorMapping, SectorPersistence
from test_strategy_iteration_shadow import shadow_env

AT = datetime(2026, 9, 30, 10, 0)
PRIMARY = ("default", "promotion", "mainline", "auction", "tenbagger", "reversal", "challenger_e")


@pytest.mark.parametrize("account", PRIMARY)
def test_primary_initial_gate_cannot_confirm_price_above_daily_high(account):
    spot = SimpleNamespace(price=10.2, prev_close=10., avg_price=10.1,
        open=10.1, high=10.15, low=10., limit_up=11., limit_down=9.)
    valid, reason, _ = paper._stable_intraday_entry_quote(spot, account_name=account)
    assert valid is False, (account, reason)


@pytest.mark.parametrize("clock,allowed", [
    (AT-timedelta(seconds=1), True), (AT, True),
    (AT+timedelta(seconds=1), False), (None, False),
    (AT-timedelta(days=1), False),
])
@pytest.mark.asyncio
async def test_c3_cannot_borrow_future_or_unknown_sector_state(shadow_env, clock, allowed):
    async with shadow_env() as db:
        db.add(StockSectorMapping(code="600001", sector_code="pw_concept_固态电池",
            sector_name="固态电池", sector_type="concept", source="pywencai",
            observed_at=AT-timedelta(days=1)))
        db.add(SectorPersistence(sector_code="pw_concept_固态电池", sector_name="固态电池",
            trade_date=AT.date(), strength_score=70., change_pct=2.,
            fund_flow=10., limit_up_count=5, consecutive_days=2, observed_at=clock))
        await db.commit()
        contexts, audit = await shadow._first_board_sector_contexts(
            db, codes={"600001"}, trade_date=AT.date(), observed_at=AT)
        assert bool(contexts) is allowed, audit
        if allowed:
            assert contexts["600001"]["sector_observed_at"] == clock.isoformat()
        else:
            assert audit["reason_counts"]["sector_clock_unproven"] == 1


@pytest.mark.parametrize("field", ["price", "prev_close", "high", "avg_price", "ask1_price",
    "ask1_volume", "volume_ratio", "orderbook_imbalance"])
def test_shadow_quote_boolean_is_not_numeric_evidence(field):
    quote = dict(price=10.2, prev_close=10., open=9.8, high=10.3, low=9.7,
        avg_price=10.1, limit_up=11., ask1_price=10.21, ask1_volume=100,
        volume_ratio=2., orderbook_imbalance=.2)
    quote[field] = True
    assert shadow._reclaim_confirmed(quote, route_id=shadow.ROUTE_B,
        min_change_pct=0., max_change_pct=5., market_change_pct=0.) is False


def test_quote_parser_preserves_real_zero_not_boolean():
    assert shadow._safe_float(0) == 0.
    assert shadow._safe_float(False) is None
    assert shadow._safe_float(True) is None
    assert shadow._safe_float("1.25") == 1.25


def test_c3_sector_visibility_rotates_only_c3_evidence(monkeypatch):
    before = {r: shadow.route_version_for(r) for r in shadow.ROUTE_IDS}
    rules = shadow._rules(shadow.ROUTE_C3)
    assert rules["sector_visibility_contract"] == "c3_sector_observed_clock_v1"
    original = shadow._rules
    def changed(route):
        result = original(route)
        if route == shadow.ROUTE_C3:
            result["sector_visibility_contract"] = "different-sector-clock"
        return result
    monkeypatch.setattr(shadow, "_rules", changed)
    after = {r: shadow.route_version_for(r) for r in shadow.ROUTE_IDS}
    assert {r for r in before if before[r] != after[r]} == {shadow.ROUTE_C3}
