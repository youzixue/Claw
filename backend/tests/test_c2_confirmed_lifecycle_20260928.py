"""C2 confirmed is historical evidence, not a token that survives a broken path."""
import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.config.settings import settings, PaperRouteSignalPolicy
from app.models.paper import PaperShadowEvent
from app.paper import strategy_iteration_shadow as shadow
from app.paper import strategy_iteration_challenger as challenger
from test_strategy_iteration_shadow import shadow_env, _seed_structures, _quote
from test_pending_buy_validity import memory_session, setup_order, reconcile, quote, AT
from test_strategy_iteration_challenger import challenger_env, _seed_confirmed
from challenger_execution_fixture import qualified_challenger_execution
from app.models.paper import PaperAutoTradeLog
from app.models.stock import StockSpot
from app.models.trading import TradeFill
from app.api.v1 import paper

START = datetime(2026, 9, 1, 9, 33)


async def scan(db, at, *, bad=False, missing=False, source_at=None, avg_price=None):
    quotes = [] if missing else [_quote(code) for code in ('600001', '600002', '600003', '600004')]
    for q in quotes:
        q['source_quote_at'] = (source_at or at).isoformat()
        if avg_price is not None and q['code'] == '600002':
            q['avg_price'] = avg_price
        if bad and q['code'] == '600002':
            q['price'] = 9.99
    await shadow.scan_strategy_iteration_shadow(db, quotes, at)


@pytest.mark.asyncio
@pytest.mark.parametrize('break_kind', ['below_vwap', 'missing', 'gap', 'vwap_slope', 'repeated_source', 'none'])
async def test_producer_preserves_confirmed_but_records_post_confirmation_boundary(shadow_env, monkeypatch, break_kind):
    monkeypatch.setattr(settings, 'PAPER_FIRST_BOARD_SHADOW_ENABLED', False)
    monkeypatch.setattr(settings, 'PAPER_ACCOUNT_ROUTE_SIGNAL_POLICIES', {
        name: PaperRouteSignalPolicy(min_relative_strength_pct=0)
        for name in ('challenger_b', 'challenger_c', 'challenger_d', 'challenger_f2')})
    monkeypatch.setattr(settings, 'ANOMALY_QUOTE_MAX_AGE_SEC', 180)
    async with shadow_env() as db:
        await _seed_structures(db)
        for sec in (0, 30, 60):
            await scan(db, START + timedelta(seconds=sec))
        event = await db.scalar(select(PaperShadowEvent).where(
            PaperShadowEvent.route_id == shadow.ROUTE_C, PaperShadowEvent.event_type == 'confirmed'))
        assert event is not None
        original = (event.event_key, event.snapshot_json, event.observed_at)
        if break_kind == 'repeated_source':
            await scan(db, START+timedelta(seconds=160), source_at=START+timedelta(seconds=60))
        elif break_kind != 'gap':
            await scan(db, START + timedelta(seconds=90), bad=break_kind=='below_vwap',
                       missing=break_kind=='missing', avg_price=10.01 if break_kind=='vwap_slope' else None)
        recovery_at = START + timedelta(seconds=180 if break_kind in ('gap', 'repeated_source') else 120)
        # New session forces all state to come from persisted evidence, not RAM.
        await db.commit()
    async with shadow_env() as db:
        await scan(db, recovery_at)
        events = list((await db.scalars(select(PaperShadowEvent).where(
            PaperShadowEvent.route_id == shadow.ROUTE_C, PaperShadowEvent.code == '600002'))).all())
        confirmed = [e for e in events if e.event_type == 'confirmed']
        assert len(confirmed) == 1
        assert (confirmed[0].event_key, confirmed[0].snapshot_json, confirmed[0].observed_at) == original
        boundaries = [e for e in events if e.event_type == 'confirmation_reset' and e.observed_at > original[2]]
        assert bool(boundaries) is (break_kind != 'none')


def reset_for(event, *, at, created=None, **changes):
    return PaperShadowEvent(**{**dict(event_key='lifecycle-reset', route_id=event.route_id,
        route_version=event.route_version, trade_date=event.trade_date, code=event.code,
        event_type='confirmation_reset', status='reset', observed_at=at, created_at=created or at,
        snapshot_json=json.dumps({'prior_structure': {'reason': 'below_vwap',
            'confirmed_path_contract': 'c2_confirmed_path_terminal_v1',
            'invalidates_event_key': event.event_key}})), **changes})


@pytest.mark.asyncio
@pytest.mark.parametrize('mismatch', ['none', 'future_observed', 'future_created', 'code', 'route', 'version', 'before', 'same_time', 'wrong_token', 'legacy_reset'])
async def test_visible_reset_is_terminal_for_original_pending_token_only(memory_session, monkeypatch, mismatch):
    db = memory_session
    order, event, risk, broker = await setup_order(db, monkeypatch)
    at = AT + timedelta(seconds=60)
    changes = {'code': '600000'} if mismatch=='code' else {'route_id': shadow.ROUTE_B} if mismatch=='route' else {'route_version': 'other'} if mismatch=='version' else {}
    reset_at = at + timedelta(seconds=1) if mismatch=='future_observed' else AT-timedelta(seconds=1) if mismatch=='before' else AT if mismatch=='same_time' else AT+timedelta(seconds=30)
    if mismatch in ('wrong_token', 'legacy_reset'):
        changes['snapshot_json'] = json.dumps({'prior_structure': {
            'confirmed_path_contract': 'c2_confirmed_path_terminal_v1' if mismatch=='wrong_token' else None,
            'invalidates_event_key': 'other-token' if mismatch=='wrong_token' else event.event_key}})
    db.add(reset_for(event, at=reset_at, created=at+timedelta(seconds=1) if mismatch=='future_created' else reset_at, **changes))
    await db.commit()
    result = await reconcile(db, monkeypatch, at=at, spot=quote(at), qualified=True)
    if mismatch == 'none':
        assert result[0]['event'] == 'canceled'
        assert 'confirmation_path' in result[0]['reason']
        assert broker.place_order.await_count == 0
        assert order.filled_quantity == 0
    else:
        assert result[0]['event'] == 'filled', result


@pytest.mark.asyncio
async def test_entry_consumer_does_not_reuse_visible_reset(shadow_env):
    # The shared async decision predicate is used by both first submit and pending.
    async with shadow_env() as db:
        event = PaperShadowEvent(event_key='confirmed', route_id=shadow.ROUTE_C,
            route_version=shadow.route_version_for(shadow.ROUTE_C), trade_date=START.date(),
            code='600002', event_type='confirmed', status='confirmed', observed_at=START,
            created_at=START, snapshot_json='{}')
        db.add_all([event, reset_for(event, at=START+timedelta(seconds=30))])
        await db.commit()
        audit = {}
        valid, reason = await challenger._confirmed_path_valid(db, event, now=START+timedelta(seconds=60), audit=audit)
        assert not valid and 'confirmation_path' in reason
        assert audit['execution_confirmation_recoverable'] is False
        assert event.event_type == 'confirmed'


@pytest.mark.asyncio
@pytest.mark.parametrize('broken', [False, True])
async def test_real_entry_window_cannot_revive_original_token(challenger_env, qualified_challenger_execution, broken):
    async with challenger_env() as db:
        event = await _seed_confirmed(db, code='600002', route_id=shadow.ROUTE_C, now=START+timedelta(seconds=60))
        early = await qualified_challenger_execution(db, now=START+timedelta(seconds=90))
        assert early['entries'] == 0
        if broken:
            db.add(reset_for(event, at=START+timedelta(seconds=90)))
        spot = await db.scalar(select(StockSpot).where(StockSpot.code=='600002'))
        spot.updated_at = START+timedelta(seconds=120)
        await db.commit()
        result = await qualified_challenger_execution(db, now=START+timedelta(seconds=120))
        assert result['entries'] == (0 if broken else 1)
        if broken:
            logs = list((await db.scalars(select(PaperAutoTradeLog).where(
                PaperAutoTradeLog.code=='600002', PaperAutoTradeLog.action=='skip_terminal'))).all())
            assert any('confirmation_path_ended' in row.reason for row in logs)


@pytest.mark.asyncio
async def test_partial_fill_is_preserved_when_original_path_ends(memory_session, monkeypatch):
    db = memory_session
    order, event, risk, broker = await setup_order(db, monkeypatch, quantity=300)
    first = await reconcile(db, monkeypatch, at=AT+timedelta(seconds=60),
                            spot=quote(AT+timedelta(seconds=60), ask1_volume=1), qualified=True)
    assert first[0]['event'] == 'partial' and order.filled_quantity == 100
    fill = await db.scalar(select(TradeFill))
    original = (fill.id, fill.price, fill.quantity, fill.commission)
    db.add(reset_for(event, at=AT+timedelta(seconds=75)))
    await db.commit()
    last = await reconcile(db, monkeypatch, at=AT+timedelta(seconds=90), round_id='recovered',
                           spot=quote(AT+timedelta(seconds=90), quote_round_id='recovered'), qualified=True)
    assert last[0]['event'] == 'canceled' and order.filled_quantity == 100
    assert (fill.id, fill.price, fill.quantity, fill.commission) == original
    assert broker.place_order.await_count == 1


@pytest.mark.asyncio
async def test_producer_continues_old_token_past_candidate_cutoff_but_not_ttl(shadow_env, monkeypatch):
    monkeypatch.setattr(settings, 'PAPER_FIRST_BOARD_SHADOW_ENABLED', False)
    monkeypatch.setattr(settings, 'PAPER_ACCOUNT_ROUTE_SIGNAL_POLICIES', {
        name: PaperRouteSignalPolicy(min_relative_strength_pct=0)
        for name in ('challenger_b', 'challenger_c', 'challenger_d', 'challenger_f2')})
    start = START.replace(hour=14, minute=29)
    async with shadow_env() as db:
        await _seed_structures(db)
        for sec in (0, 30, 60, 90, 120):
            await scan(db, start+timedelta(seconds=sec))
        rows = list((await db.scalars(select(PaperShadowEvent).where(
            PaperShadowEvent.route_id==shadow.ROUTE_C, PaperShadowEvent.code=='600002'))).all())
        assert len([r for r in rows if r.event_type=='confirmed']) == 1
        assert not [r for r in rows if r.event_type=='confirmation_reset']
        await scan(db, start+timedelta(minutes=14))
        after = list((await db.scalars(select(PaperShadowEvent).where(
            PaperShadowEvent.route_id==shadow.ROUTE_C, PaperShadowEvent.code=='600002'))).all())
        assert len(after) == len(rows), 'expired token must not create all-afternoon samples'
