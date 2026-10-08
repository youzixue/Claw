"""Primary quota consumers: real receipts, isolated DB; never alter ledger facts."""
import inspect
import json
from datetime import timedelta

import pytest
from sqlalchemy import event, select

from app.api.v1 import paper
from app.models.paper import PaperAccount, PaperAutoTradeLog, PaperPosition
from app.models.stock import StockTag, StockSpot, MarketSentiment
from app.models.trading import TradeFill
from app.paper import experiment
from test_paper_api import paper_client
from test_paper_fill_identity_20260923 import AT, DAY, seed

PRIMARY = {"default", "promotion", "mainline", "auction", "tenbagger", "reversal", "challenger_e"}
MISSING = object()


async def logs_for(db, account, trades, *, sectors=None, scale=MISSING):
    logs = []
    for i, trade in enumerate(trades):
        candidate = {"sector_name": sectors[i] if sectors is not None else "电力"}
        if scale is not MISSING:
            candidate["scale_in"] = scale
        log = PaperAutoTradeLog(
            account_id=account.id, code=trade.code, strategy_version=trade.strategy_version,
            executed_trade_id=trade.id, trade_date=DAY, created_at=AT,
            run_id=f"quota-{trade.id}-{i}", source="promotion_promotion",
            action="buy", decision="executed", candidate_json=json.dumps(candidate),
        )
        db.add(log)
        logs.append(log)
    await db.commit()
    return logs


async def quota(db, account, **kw):
    return await paper._today_auto_new_buy_logs(db, DAY, account_id=account.id, as_of=AT, **kw)


@pytest.mark.asyncio
@pytest.mark.parametrize("name", sorted(PRIMARY))
async def test_real_two_fills_one_primary_daily_and_sector_slot(paper_client, monkeypatch, name):
    _, maker = paper_client
    monkeypatch.setattr(paper, "_public_order_clock", lambda: AT)
    async with maker() as db:
        account, order, trades, fills = await seed(db, account_name=name, scale=False)
        logs = await logs_for(db, account, trades)
        before = [paper._trade_payload(t) for t in trades]
        # Default physical clock path also gives an assertion red on the old helper.
        rows = await paper._today_auto_new_buy_logs(db, DAY, account_id=account.id)
        assert [r.id for r in rows] == [logs[0].id]
        assert paper._sector_counts_from_auto_logs(rows) == {"电力": 1}
        assert [paper._trade_payload(t) for t in trades] == before
        assert order.quantity == order.filled_quantity == 300 and len(fills) == 2
        assert len(list((await db.scalars(select(PaperAutoTradeLog))).all())) == 2
        assert not db.dirty and not db.new and not db.deleted


@pytest.mark.asyncio
async def test_same_stock_signal_different_orders_keep_two_slots(paper_client):
    _, maker = paper_client
    async with maker() as db:
        account, _, first, _ = await seed(db, account_name="promotion", scale=False, order_key="one")
        await logs_for(db, account, first)
        _, _, second, _ = await seed(db, account_name="promotion", scale=False, order_key="two")
        await logs_for(db, account, second)
        rows = await quota(db, account)
        assert len(rows) == 2
        assert paper._sector_counts_from_auto_logs(rows) == {"电力": 2}


@pytest.mark.asyncio
@pytest.mark.parametrize("sectors,expected", [
    (["电力", "电力"], 1), (["", ""], 1), (["电力", "银行"], 2), (["", "电力"], 2),
])
async def test_sector_conflicts_keep_whole_group(paper_client, sectors, expected):
    _, maker = paper_client
    async with maker() as db:
        account, _, trades, _ = await seed(db, account_name="promotion", scale=False)
        await logs_for(db, account, trades, sectors=sectors)
        rows = await quota(db, account)
        assert len(rows) == expected
        assert paper._sector_counts_from_auto_logs(rows) == {
            s: sectors.count(s) if expected == 2 else 1 for s in set(sectors) if s
        }


@pytest.mark.asyncio
@pytest.mark.parametrize("order_scale,log_scale,expected", [
    (True, MISSING, 0), (True, True, 0), (True, False, 2),
    (False, True, 2), (None, True, 1), (True, "true", 2), (True, 1, 2),
    (None, MISSING, 1), (False, False, 1),
])
async def test_scale_marker_requires_original_authority(paper_client, order_scale, log_scale, expected):
    _, maker = paper_client
    async with maker() as db:
        account, _, trades, _ = await seed(db, account_name="default", scale=order_scale)
        await logs_for(db, account, trades, scale=log_scale)
        assert len(await quota(db, account)) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", [
    "log_code", "log_version", "log_round", "log_bool", "fill_price", "fill_raw",
    "fill_future", "duplicate_receipt", "wrong_order", "missing_receipt", "missing_trade",
    "foreign_order", "overclaimed", "duplicate_account",
])
async def test_conflict_never_hides_behind_valid_scale_sibling(paper_client, mutation):
    _, maker = paper_client
    async with maker() as db:
        account, order, trades, fills = await seed(db, account_name="default", scale=True)
        logs = await logs_for(db, account, trades)
        if mutation == "log_code":
            logs[0].code = "600999"
        elif mutation == "log_version":
            logs[0].strategy_version = "wrong"
        elif mutation == "log_round":
            logs[0].created_at = order.decision_at
            logs[0].quote_round_id = "wrong"
        elif mutation == "log_bool":
            logs[0].candidate_json = '{"scale_in": false, "sector_name": "电力"}'
        elif mutation == "fill_price":
            fills[0].price += .1
        elif mutation == "fill_raw":
            fills[0].raw_json = "{}"
        elif mutation == "fill_future":
            fills[0].filled_at = AT + timedelta(seconds=1)
        elif mutation == "duplicate_receipt":
            db.add(TradeFill(fill_id="duplicate", order_id=order.order_id, broker="paper",
                code=order.code, side="buy", price=10, quantity=100,
                broker_trade_id=str(trades[0].id), filled_at=trades[0].trade_time,
                trade_date=DAY, raw_json=fills[0].raw_json))
        elif mutation == "wrong_order":
            fills[0].order_id = "absent"
        elif mutation == "missing_receipt":
            await db.delete(fills[0])
        elif mutation == "missing_trade":
            logs[0].executed_trade_id = 999999
        elif mutation == "foreign_order":
            order.account_id = "promotion"
        elif mutation == "overclaimed":
            order.filled_quantity = 200
        else:
            db.add(PaperAccount(account_name="default", initial_capital=50000))
        await db.commit()
        rows = await quota(db, account)
        # Untraceable missing/wrong receipt has no authority to taint another
        # proven order: its own unknown log remains; identifiable conflicts taint all.
        expected = 1 if mutation in {"missing_receipt", "wrong_order", "missing_trade"} else 2
        assert len(rows) == expected


@pytest.mark.asyncio
async def test_duplicate_log_trade_dedup_and_conflict_taints_siblings(paper_client):
    _, maker = paper_client
    async with maker() as db:
        account, _, trades, _ = await seed(db, account_name="promotion", scale=False)
        await logs_for(db, account, trades)
        duplicate = (await logs_for(db, account, trades[:1]))[0]
        assert len(await quota(db, account)) == 1
        duplicate.code = "wrong"
        await db.commit()
        assert len(await quota(db, account)) == 3


@pytest.mark.asyncio
async def test_physical_asof_and_round_clock_exception(paper_client, monkeypatch):
    _, maker = paper_client
    async with maker() as db:
        account, order, trades, _ = await seed(db, account_name="default", scale=False)
        logs = await logs_for(db, account, trades)
        for log in logs:
            log.created_at = order.decision_at
            log.quote_round_id = order.decision_round_id
        await db.commit()
        monkeypatch.setattr(paper, "_paper_now", lambda: order.decision_at)
        monkeypatch.setattr(paper, "_public_order_clock", lambda: AT)
        assert len(await paper._today_auto_new_buy_logs(db, DAY, account_id=account.id)) == 1
        # Visible audit referring to a not-yet-visible ledger cannot free quota.
        early = trades[0].trade_time - timedelta(seconds=1)
        assert len(await paper._today_auto_new_buy_logs(db, DAY, account_id=account.id, as_of=early)) == 2
        logs[1].created_at = AT + timedelta(seconds=1)
        await db.commit()
        assert len(await quota(db, account)) == 1
        assert await paper._today_auto_new_buy_logs(
            db, DAY, account_id=account.id, as_of=AT-timedelta(days=1)) == []


@pytest.mark.asyncio
async def test_null_unknown_account_and_foreign_pointer_are_not_exempt(paper_client):
    _, maker = paper_client
    async with maker() as db:
        account, _, trades, _ = await seed(db, account_name="default", scale=True)
        null_logs = await logs_for(db, account, trades, scale=True)
        for log in null_logs:
            log.account_id = None
        other, _, other_trades, _ = await seed(db, account_name="promotion", scale=True, order_key="other")
        foreign = (await logs_for(db, account, other_trades[:1], scale=True))[0]
        await db.commit()
        assert len(await quota(db, account)) == 1
        assert len(await quota(db, account, include_legacy_null=True)) == 3
        assert await quota(db, other) == []
        assert len(await paper._today_auto_new_buy_logs(db, DAY, as_of=AT)) == 3
        assert foreign.executed_trade_id == other_trades[0].id


@pytest.mark.asyncio
async def test_legacy_true_without_receipts_cannot_exempt(paper_client):
    _, maker = paper_client
    async with maker() as db:
        account, _, trades, fills = await seed(db, account_name="promotion", scale=True)
        await logs_for(db, account, trades, scale=True)
        for fill in fills:
            await db.delete(fill)
        await db.commit()
        assert len(await quota(db, account)) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("slices", [(100,), (100, 200)])
async def test_canceled_partial_order_still_counts_once(paper_client, slices):
    _, maker = paper_client
    async with maker() as db:
        account, order, trades, _ = await seed(db, account_name="promotion", scale=False, quantities=slices)
        await logs_for(db, account, trades)
        order.quantity += 100
        order.status = "canceled"
        await db.commit()
        assert len(await quota(db, account)) == 1
        # A canceled zero-fill order has no executed log and does not enter quota.
        _, zero, _, _ = await seed(db, account_name="promotion", order_key="zero", quantities=())
        zero.quantity = 100
        zero.status = "canceled"
        await db.commit()
        assert len(await quota(db, account)) == 1


@pytest.mark.asyncio
async def test_more_than_one_in_batch_and_tail_conflict(paper_client):
    _, maker = paper_client
    async with maker() as db:
        account, _, trades, fills = await seed(
            db, account_name="promotion", scale=False, quantities=(100,) * 405)
        # Seed clocks spread over minutes: use a physical cutoff covering them all.
        await logs_for(db, account, trades)
        cutoff = max(t.trade_time for t in trades) + timedelta(seconds=1)
        rows = list((await db.scalars(select(PaperAutoTradeLog))).all())
        for log in rows:
            log.created_at = cutoff
        await db.commit()
        calls = []
        bind = db.bind.sync_engine
        def capture(conn, cursor, statement, parameters, context, executemany):
            if statement.lstrip().upper().startswith("SELECT"):
                calls.append(statement)
        event.listen(bind, "before_cursor_execute", capture)
        try:
            result = await paper._today_auto_new_buy_logs(db, DAY, account_id=account.id, as_of=cutoff)
        finally:
            event.remove(bind, "before_cursor_execute", capture)
        assert len(result) == 1
        assert len(calls) < 20  # bounded batches, not N+1 per log
        fills[-1].raw_json = "{}"
        await db.commit()
        assert len(await paper._today_auto_new_buy_logs(
            db, DAY, account_id=account.id, as_of=cutoff)) == 405


@pytest.mark.parametrize("standard", [False, True])
def test_only_seven_primary_identities_rotate(monkeypatch, standard):
    versioner = experiment.standard_execution_version if standard else experiment.execution_version
    before = {n: versioner("fixed", n) for n in experiment.EXPERIMENT_ACCOUNTS}
    identities = {n: experiment.execution_signal_identity(n) for n in before}
    monkeypatch.setattr(experiment, "PRIMARY_NEW_BUY_QUOTA_CONTRACT_VERSION", "test-next")
    after = {n: versioner("fixed", n) for n in before}
    assert {n for n in before if before[n] != after[n]} == PRIMARY
    for name in before:
        old = identities[name]
        new = experiment.execution_signal_identity(name)
        assert {k for k in set(old) | set(new) if old.get(k) != new.get(k)} == (
            {"primary_new_buy_quota_contract"} if name in PRIMARY else set())


@pytest.mark.asyncio
async def test_missing_order_marker_with_conflicting_log_markers_is_not_collapsed(paper_client):
    _, maker = paper_client
    async with maker() as db:
        account, _, trades, _ = await seed(db, account_name="promotion", scale=None)
        logs = await logs_for(db, account, trades, scale=True)
        logs[-1].candidate_json = '{"scale_in": false, "sector_name": "电力"}'
        await db.commit()
        assert len(await quota(db, account)) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["wrong_day", "wrong_side", "old_version", "future_log"])
async def test_day_side_version_and_future_audit_boundaries(paper_client, mutation):
    _, maker = paper_client
    async with maker() as db:
        account, _, trades, _ = await seed(db, account_name="promotion", scale=False)
        logs = await logs_for(db, account, trades)
        if mutation == "wrong_day":
            trades[0].trade_time -= timedelta(days=1)
        elif mutation == "wrong_side":
            trades[0].trade_type = "sell"
        elif mutation == "future_log":
            for log in logs:
                log.created_at = AT + timedelta(seconds=1)
        # Seed identity-test-v1 intentionally differs from current execution version.
        await db.commit()
        expected = {"wrong_day": 2, "wrong_side": 2, "old_version": 1, "future_log": 0}[mutation]
        assert len(await quota(db, account)) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("two_orders", [False, True])
async def test_primary_capacity_consumer_keeps_real_limits(paper_client, monkeypatch, two_orders):
    """Real consumer, receipt helper and risk engine; dry-run never submits."""
    from app.risk.engine import risk_engine
    from app.risk.rules import register_all_rules
    from app.trading import service
    from unittest.mock import AsyncMock
    monkeypatch.setattr(risk_engine, "_rules", [])
    register_all_rules()
    monkeypatch.setattr(paper, "_paper_now", lambda: AT)
    monkeypatch.setattr(paper, "_public_order_clock", lambda: AT)
    monkeypatch.setattr(paper, "_is_afternoon_new_buy_time", lambda *_a: False)
    monkeypatch.setattr(paper, "_is_late_new_buy_time", lambda *_a: False)
    submit = AsyncMock(side_effect=AssertionError("dry-run must not submit"))
    monkeypatch.setattr(service, "submit_order", submit)
    async def candidates(*_args, **_kwargs):
        return [{
            "code": "600888", "name": "合规候选", "_source": "promotion_promotion",
            "signal_source": "promotion_promotion", "total_score": 95,
            "sector_name": "电力", "change_pct": .5, "stop_loss_price": 9.2,
            "trade_gate_passed": True, "actionable": True, "watch_only": False,
        }], []
    monkeypatch.setattr(paper, "_promotion_route_buy_candidates", candidates)
    _, maker = paper_client
    async with maker() as db:
        account, _, trades, _ = await seed(db, account_name="promotion", scale=False, order_key="one")
        await logs_for(db, account, trades)
        if two_orders:
            _, _, more, _ = await seed(db, account_name="promotion", scale=False, order_key="two")
            await logs_for(db, account, more)
        db.add(PaperPosition(account_id=account.id, code="600001", name="旧仓",
            buy_price=10, buy_amount=300, buy_time=AT-timedelta(minutes=5),
            current_price=10, is_closed=False, strategy_version="identity-test-v1"))
        db.add(StockTag(code="600888", name="合规候选", board_type="main_sh", board_tag="tradeable"))
        db.add(StockSpot(code="600888", name="合规候选", price=10, open=9.95,
            high=10.2, low=9.8, avg_price=10, change_pct=.5, volume_ratio=1.2,
            ask1_price=10.01, bid1_price=9.99, limit_up=11, limit_down=9, updated_at=AT))
        db.add(MarketSentiment(trade_date=DAY, sentiment_cycle="recovery", sentiment_score=60,
            limit_up_count=40, limit_down_count=5, broken_limit_count=8, seal_rate=70,
            board_height=3, advance_decline_ratio=1.2, turnover_total=1.2,
            main_net_inflow=20, quality_status="ok", quality_reason="",
            calculation_version="isolated_quota_test"))
        await db.commit()
        result = await paper.run_paper_auto_trade(
            db, execute=False, trigger="quota-test", max_candidates=1,
            execution_mode="manual", account_name="promotion", include_position_risk=False, now=AT)
        dry = [r for r in result["logs"] if r["action"] == "buy" and r["decision"] == "dry_run"]
        assert len(dry) == (0 if two_orders else 1), result["logs"]
        if two_orders:
            assert any("策略日限2只" in r["reason"] for r in result["logs"]), result["logs"]
        assert not submit.await_count


def test_primary_callsite_uses_physical_clock_and_one_quota_projection():
    source = inspect.getsource(paper)
    call = source[source.index("today_new_buy_logs = await"):source.index("today_new_buy_count = len")]
    assert "as_of=_public_order_clock()," in call
    assert "today_new_buy_count = len(today_new_buy_logs)" in source
    assert "today_sector_counts = _sector_counts_from_auto_logs(today_new_buy_logs)" in source
