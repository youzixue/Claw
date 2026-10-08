"""No repeated source tick may manufacture a primary persistent path."""
import json
from datetime import datetime, timedelta
import pytest
from app.api.v1 import paper
from app.models.paper import PaperAutoTradeLog
from test_paper_deferred_exit_provenance import memory_session

AT = datetime(2026, 9, 30, 10)

@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ('repeat', 'missing', 'future', 'previous_day', 'malformed'))
async def test_unproven_source_ticks_do_not_confirm(memory_session, monkeypatch, kind):
    monkeypatch.setattr(paper, 'account_confirmation_policy', lambda _: dict(
        min_samples=2, min_persistence_sec=60, max_sample_gap_sec=90, clock_jitter_sec=0))
    db = memory_session
    for seconds in (60, 30):
        at = AT-timedelta(seconds=seconds)
        source = {'repeat': AT.isoformat(), 'missing': None,
                  'future': (AT+timedelta(seconds=1)).isoformat(),
                  'previous_day': (AT-timedelta(days=1)).isoformat(),
                  'malformed': 'bad'}[kind]
        db.add(PaperAutoTradeLog(account_id=2, trade_date=AT.date(), code='600001',
            source='next_day_plan', action='confirm_buy', decision='wait', run_id='test',
            trigger='test', reason='test', created_at=at,
            strategy_version=paper._strategy_version('default'),
            candidate_json=json.dumps(dict(confirmation_version='champion_persistent_v1',
                confirmation_sample_at=at.isoformat(), confirmation_source_quote_at=source))))
    await db.flush()
    ready, count, duration = await paper._champion_intraday_confirmation_status(db,
        account_id=2, trade_date=AT.date(), code='600001', source='next_day_plan', current_at=AT)
    assert (ready, count, duration) == (False, 1, 0.)

@pytest.mark.asyncio
async def test_distinct_visible_source_ticks_confirm(memory_session, monkeypatch):
    monkeypatch.setattr(paper, 'account_confirmation_policy', lambda _: dict(
        min_samples=2, min_persistence_sec=60, max_sample_gap_sec=90, clock_jitter_sec=0))
    db = memory_session
    tick = AT-timedelta(seconds=60)
    db.add(PaperAutoTradeLog(account_id=2, trade_date=AT.date(), code='600001',
        source='next_day_plan', action='confirm_buy', decision='wait', run_id='test',
        trigger='test', reason='test', created_at=tick+timedelta(seconds=2),
        strategy_version=paper._strategy_version('default'),
        candidate_json=json.dumps(dict(confirmation_version='champion_persistent_v1',
            confirmation_sample_at=(tick+timedelta(seconds=2)).isoformat(),
            confirmation_source_quote_at=tick.isoformat()))))
    await db.flush()
    assert await paper._champion_intraday_confirmation_status(db, account_id=2,
        trade_date=AT.date(), code='600001', source='next_day_plan', current_at=AT
    ) == (True, 2, 60.)

@pytest.mark.asyncio
@pytest.mark.parametrize('reset', (True, False))
@pytest.mark.parametrize('portfolio_only', (False, True))
async def test_invalid_path_resets_but_capacity_wait_does_not(memory_session, monkeypatch, reset, portfolio_only):
    monkeypatch.setattr(paper, 'account_confirmation_policy', lambda _: dict(
        min_samples=2, min_persistence_sec=60, max_sample_gap_sec=90, clock_jitter_sec=0))
    db = memory_session
    base = dict(account_id=2, trade_date=AT.date(), code='600001', source='next_day_plan',
        run_id='test', trigger='test', reason='test', decision='wait',
        strategy_version=paper._strategy_version('default'))
    tick = AT-timedelta(seconds=60)
    db.add(PaperAutoTradeLog(**base, action='portfolio_confirm' if portfolio_only else 'confirm_buy', created_at=tick,
        candidate_json=json.dumps(dict(confirmation_version='champion_persistent_v1',
            confirmation_source_quote_at=tick.isoformat()))))
    await db.flush()
    db.add(PaperAutoTradeLog(**base, action='portfolio_skip' if portfolio_only else 'skip_buy', created_at=AT-timedelta(seconds=30),
        candidate_json=json.dumps(dict(primary_confirmation_reset=reset))))
    await db.flush()
    result = await paper._champion_intraday_confirmation_status(db, account_id=2,
        trade_date=AT.date(), code='600001', source='next_day_plan', current_at=AT,
        portfolio_only=portfolio_only)
    assert result == ((False, 1, 0.) if reset else (True, 2, 60.))
