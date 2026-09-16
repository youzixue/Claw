"""Delivery-time regression: isolated SQLite and mocked Feishu only."""
import json
from datetime import timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.push import paper_buy_points as points
from test_paper_buy_points import setup, record, logs


def advancing_clock(monkeypatch):
    base = points.datetime
    offset = {'seconds': 0}
    class Clock(base):
        @classmethod
        def now(cls, tz=None):
            return base.now(tz) + timedelta(seconds=offset['seconds'])
    monkeypatch.setattr(points, 'datetime', Clock)
    return offset


@pytest.mark.asyncio
async def test_network_completion_is_not_poll_start_and_not_user_receipt(setup, monkeypatch):
    maker, now, send = setup
    await record(maker, now)
    clock = advancing_clock(monkeypatch)
    async def network(*args, **kwargs):
        clock['seconds'] = 12
        return {'channels': {'feishu': True}, 'status': 'sent'}
    send.side_effect = network
    assert (await points.dispatch_buy_points(now=now, session_factory=maker))['status'] == 'sent'
    delivery = (await logs(maker))[-1]
    payload = json.loads(delivery.candidate_json)
    assert delivery.created_at == now + timedelta(seconds=12)
    assert payload['delivery_clock_schema'] == 'paper_push_transport_v1'
    assert payload['dispatch_started_at'] == now.isoformat()
    assert payload['send_started_at'] == now.isoformat()
    assert payload['send_completed_at'] == (now + timedelta(seconds=12)).isoformat()
    assert payload['user_received_at'] is None


@pytest.mark.asyncio
async def test_expired_during_lease_commit_never_reaches_network(setup, monkeypatch):
    maker, now, send = setup
    await record(maker, now)
    clock = advancing_clock(monkeypatch)
    class SlowCommit(AsyncSession):
        async def commit(self):
            await super().commit()
            clock['seconds'] = 181
    slow_maker = async_sessionmaker(maker.kw['bind'], class_=SlowCommit, expire_on_commit=False)
    result = await points.dispatch_buy_points(now=now, session_factory=slow_maker)
    assert not send.called
    assert result['count'] == 0
    delivery = (await logs(maker))[-1]
    assert delivery.decision == 'expired'
    assert delivery.created_at == now + timedelta(seconds=181)


@pytest.mark.asyncio
async def test_each_stock_source_clock_rechecked_after_database_delay(setup, monkeypatch):
    maker, now, send = setup
    await record(maker, now, market_context={
        'price': 10.5, 'quote_round_id': 'round-current',
        'source_quote_at': (now-timedelta(seconds=175)).isoformat()})
    clock = advancing_clock(monkeypatch)
    class SlowCommit(AsyncSession):
        async def commit(self):
            await super().commit()
            clock['seconds'] = 6
    slow_maker = async_sessionmaker(maker.kw['bind'], class_=SlowCommit, expire_on_commit=False)
    await points.dispatch_buy_points(now=now, session_factory=slow_maker)
    assert not send.called
    assert (await logs(maker))[-1].decision == 'expired'


@pytest.mark.asyncio
async def test_network_failure_records_actual_completion_not_fabricated_delivery(setup, monkeypatch):
    maker, now, send = setup
    await record(maker, now)
    clock = advancing_clock(monkeypatch)
    async def network(*args, **kwargs):
        clock['seconds'] = 7
        raise TimeoutError('fake network timeout')
    send.side_effect = network
    result = await points.dispatch_buy_points(now=now, session_factory=maker)
    assert result['status'] == 'failed'
    delivery = (await logs(maker))[-1]
    assert delivery.created_at == now + timedelta(seconds=7)
    assert json.loads(delivery.candidate_json)['user_received_at'] is None


@pytest.mark.asyncio
async def test_one_expired_stock_does_not_drop_fresh_account_signal(setup, monkeypatch):
    maker, now, send = setup
    await record(maker, now, account='default', market_context={
        'price': 10.5, 'quote_round_id': 'round-current',
        'source_quote_at': (now-timedelta(seconds=175)).isoformat()})
    await record(maker, now, account='challenger_b')
    clock = advancing_clock(monkeypatch)
    class SlowCommit(AsyncSession):
        async def commit(self):
            await super().commit()
            clock['seconds'] = 6
    slow_maker = async_sessionmaker(maker.kw['bind'], class_=SlowCommit, expire_on_commit=False)
    result = await points.dispatch_buy_points(now=now, session_factory=slow_maker)
    assert result['count'] == 1
    assert '策略-challenger_b' in send.call_args.args[0].content
    assert '策略-default' not in send.call_args.args[0].content
    delivered = [row for row in await logs(maker) if row.action == points.DELIVERY]
    assert sum(row.decision == 'sent' for row in delivered) == 1
    assert sum(row.decision == 'expired' for row in delivered) == 1


@pytest.mark.parametrize('seconds,valid', [(180, True), (180.001, False), (-0.001, False)])
def test_freshness_boundary_keeps_original_ttl_and_future_rejection(seconds, valid):
    from datetime import datetime
    now = datetime(2026, 9, 9, 10)
    item = {'trade_date': now.date(), 'created_at': now, 'as_of_at': now,
            'payload': {'market_context': {'source_quote_at': now.isoformat()}}}
    assert points._delivery_fresh(item, now+timedelta(seconds=seconds)) is valid
