"""Original-order counting; synthetic ledgers stay in the pytest temporary DB."""
import json
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from app.api.v1 import paper
from app.models.paper import PaperAccount, PaperAutoTradeLog, PaperTradeLog
from app.models.trading import TradeFill, TradeOrder
from app.paper.strategy_iteration_challenger import _today_buy_count
from app.trading.service import _paper_fill_request_id
from test_paper_api import paper_client
from test_strategy_iteration_challenger import challenger_env
from challenger_execution_fixture import qualified_challenger_execution

AT = datetime(2026, 9, 23, 11)
DAY = AT.date()


async def seed(db, *, account_name="challenger_b", scale=True, order_key="parent",
               code="600001", quantities=(100, 200)):
    account = await db.scalar(select(PaperAccount).where(PaperAccount.account_name == account_name))
    if account is None:
        account = PaperAccount(account_name=account_name, strategy=account_name,
                               initial_capital=50000, current_capital=45000, total_assets=50000)
        db.add(account)
        await db.flush()
    order = TradeOrder(
        order_id=order_key, broker="paper", account_id=account_name, code=code,
        side="buy", order_type="limit", price=10, quantity=sum(quantities),
        filled_quantity=0, status="partial", signal_id="original-confirmation",
        strategy_version="identity-test-v1", decision_round_id="decision",
        trade_date=DAY, created_at=AT-timedelta(minutes=10), decision_at=AT-timedelta(minutes=10),
        risk_json=json.dumps({"paper_deferred_order": {"candidate": {"scale_in": scale}}}),
    )
    db.add(order)
    trades, fills = [], []
    for index, quantity in enumerate(quantities):
        clock = AT-timedelta(minutes=5-index)
        round_id = f"{order_key}-fill-{index}"
        request = _paper_fill_request_id(order, round_id=round_id)
        trade = PaperTradeLog(
            account_id=account.id, code=code, trade_type="buy", price=10, amount=quantity,
            trade_time=clock, signal_id="chlg-"+request if account_name.startswith("challenger") else request,
            strategy_version=order.strategy_version, decision_round_id="decision",
            fill_round_id=round_id, commission=5, tax=0,
        )
        db.add(trade)
        await db.flush()
        fill = TradeFill(
            fill_id="fill-"+request, order_id=order_key, broker="paper", code=code,
            side="buy", price=10, quantity=quantity, commission=5, tax=0,
            broker_trade_id=str(trade.id), filled_at=clock, trade_date=DAY,
            decision_round_id="decision", fill_round_id=round_id,
            raw_json=json.dumps({**paper._trade_payload(trade), "pending_execution_timing": {
                "contract_version": "pending_paper_fill_timing_v1_20260914",
                "mandatory": True, "status": "validated", "order_id": order_key,
                "account_id": account_name, "code": code, "side": "buy",
                "quote_round_id": round_id, "request_id": request,
                "fill_price": trade.price, "filled_quantity": quantity,
            }}),
        )
        db.add(fill)
        order.filled_quantity += quantity
        trades.append(trade)
        fills.append(fill)
    order.status = "filled"
    await db.commit()
    return account, order, trades, fills


@pytest.mark.asyncio
@pytest.mark.parametrize("max_daily,expected", [(1, 1), (2, 2)])
async def test_live_day_quota_includes_fills_after_round_start(
    challenger_env, qualified_challenger_execution, monkeypatch, max_daily, expected,
):
    from test_strategy_iteration_challenger import _seed_confirmed
    from app.paper.strategy_iteration_shadow import ROUTE_C
    now = datetime(2026, 9, 1, 10, 0)
    physical = now + timedelta(seconds=2)
    monkeypatch.setattr(paper, "_public_order_clock", lambda: physical)
    monkeypatch.setattr(paper, "_strategy_buy_limits", lambda _name: (max_daily, 3))
    async with challenger_env() as db:
        for code in ("600107", "600108"):
            await _seed_confirmed(db, code=code, route_id=ROUTE_C, now=now,
                                  price=10.03, ask=10.04, volume_ratio=2.5, orderbook_imbalance=1.0)
        result = await qualified_challenger_execution(db, now=now)
        assert result["entries"] == expected
        account = await paper._get_or_create_account(db, "challenger_c")
        rows = list((await db.scalars(select(PaperTradeLog).where(
            PaperTradeLog.account_id == account.id, PaperTradeLog.trade_type == "buy"))).all())
        assert len(rows) == expected and all(row.trade_time == physical for row in rows)
        assert await _today_buy_count(db, account.id, now.date(), as_of=physical) == expected


@pytest.mark.parametrize("standard", [False, True])
def test_counting_version_rotates_only_the_six_execution_consumers(monkeypatch, standard):
    from app.paper import experiment
    expected = {"default", "challenger_a", "challenger_b", "challenger_c", "challenger_d", "challenger_f2"}
    versioner = experiment.standard_execution_version if standard else experiment.execution_version
    before = {name: versioner("fixed", name) for name in experiment.EXPERIMENT_ACCOUNTS}
    monkeypatch.setattr(experiment, "BUY_ORDER_COUNT_CONTRACT_VERSION", "changed-only-for-test")
    after = {name: versioner("fixed", name) for name in experiment.EXPERIMENT_ACCOUNTS}
    assert {name for name in before if before[name] != after[name]} == expected


@pytest.mark.asyncio
async def test_two_actual_request_ids_one_order_are_one_layer(paper_client):
    _, maker = paper_client
    async with maker() as db:
        account, _, trades, _ = await seed(db, account_name="default", scale=False)
        assert trades[0].signal_id != trades[1].signal_id
        before = [paper._trade_payload(t) for t in trades]
        layers, last = await paper._same_day_buy_layers(db, account_id=account.id, code="600001", now=AT)
        assert (layers, last) == (1, trades[-1].trade_time)
        assert [paper._trade_payload(t) for t in trades] == before
        assert not db.dirty and not db.new and not db.deleted


@pytest.mark.asyncio
async def test_closed_same_name_history_does_not_merge_or_block_active_wallet(paper_client):
    _, maker = paper_client
    async with maker() as db:
        archived = PaperAccount(account_name="default", initial_capital=1000000, status="closed")
        db.add(archived)
        await db.flush()
        archived_id = archived.id
        # The current live layout really has a closed default before active default.
        active = PaperAccount(account_name="default", initial_capital=50000, status="active")
        db.add(active)
        await db.commit()
        # seed() normally resolves a name; explicit rename is test setup only so it
        # generates the same strict receipt chain in the active numeric account.
        archived.account_name = "archived-fixture"
        await db.commit()
        account, _, _, _ = await seed(db, account_name="default", scale=False)
        archived.account_name = "default"
        db.add(PaperTradeLog(account_id=archived_id, code="600001", trade_type="buy",
            price=10, amount=100, trade_time=AT, signal_id="archived-do-not-merge"))
        await db.commit()
        assert account.id == active.id
        assert (await paper._same_day_buy_layers(db, account_id=account.id, code="600001", now=AT))[0] == 1
        assert archived.status == "closed" and archived.initial_capital == 1000000


@pytest.mark.asyncio
async def test_same_original_signal_on_two_distinct_orders_is_two_layers(paper_client):
    _, maker = paper_client
    async with maker() as db:
        account, _, _, _ = await seed(db, quantities=(100,), order_key="one")
        await seed(db, quantities=(100,), order_key="two")
        assert (await paper._same_day_buy_layers(db, account_id=account.id, code="600001", now=AT))[0] == 2


@pytest.mark.asyncio
async def test_cross_day_position_topup_partial_receipts_are_not_new_name(paper_client):
    _, maker = paper_client
    async with maker() as db:
        account, _, trades, _ = await seed(db)
        # Prior-day first buy is not a current-day new position; no fabricated fills.
        db.add(PaperTradeLog(account_id=account.id, code="600001", trade_type="buy",
                            price=10, amount=100, trade_time=AT-timedelta(days=1), signal_id="prior"))
        await db.commit()
        assert await _today_buy_count(db, account.id, DAY) == 0
        assert (await paper._same_day_buy_layers(db, account_id=account.id, code="600001", now=AT))[0] == 1
        assert len(trades) == 2


@pytest.mark.asyncio
async def test_canceled_remainder_does_not_erase_two_completed_slices(paper_client):
    _, maker = paper_client
    async with maker() as db:
        account, order, trades, _ = await seed(db)
        order.quantity += 100
        order.status = "canceled"
        await db.commit()
        assert await _today_buy_count(db, account.id, DAY, as_of=AT) == 0
        assert await paper._same_day_buy_layers(db, account_id=account.id, code="600001", now=AT) == (
            1, trades[-1].trade_time)


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["none", "code", "version", "explicit_false"])
async def test_exact_legacy_or_conflicting_audit_is_scoped_to_its_receipt(paper_client, mutation):
    _, maker = paper_client
    async with maker() as db:
        account, _, trades, fills = await seed(db, quantities=(100,))
        if mutation == "none":
            await db.delete(fills[0])  # exact legacy executed_trade_id, not order signal guess
        db.add(PaperAutoTradeLog(account_id=account.id,
            code="600002" if mutation == "code" else "600001", action="buy",
            decision="executed", run_id="exact", created_at=AT, trade_date=DAY,
            strategy_version="bad" if mutation == "version" else trades[0].strategy_version,
            executed_trade_id=trades[0].id,
            candidate_json=json.dumps({"scale_in": mutation != "explicit_false"})))
        await db.commit()
        assert await _today_buy_count(db, account.id, DAY, as_of=AT) == (0 if mutation == "none" else 1)


@pytest.mark.asyncio
async def test_same_day_initial_buy_is_not_erased_by_later_topup(paper_client):
    _, maker = paper_client
    async with maker() as db:
        account, _, _, _ = await seed(db, scale=False, order_key="initial", quantities=(100,))
        await seed(db, scale=True, order_key="topup", quantities=(100,))
        assert await _today_buy_count(db, account.id, DAY) == 1


@pytest.mark.asyncio
async def test_unknown_legacy_same_signal_cannot_collapse_rows_or_spread_scale_marker(paper_client):
    _, maker = paper_client
    async with maker() as db:
        account = PaperAccount(account_name="challenger_b", initial_capital=50000)
        db.add(account)
        await db.flush()
        rows = [PaperTradeLog(account_id=account.id, code="600001", trade_type="buy",
                              price=10, amount=100, trade_time=AT, signal_id="same-text")
                for _ in range(2)]
        db.add_all(rows)
        await db.flush()
        db.add(PaperAutoTradeLog(account_id=account.id, code="600001", action="buy",
            decision="executed", run_id="legacy", created_at=AT, trade_date=DAY,
            executed_trade_id=rows[0].id, candidate_json=json.dumps({"scale_in": True})))
        await db.commit()
        assert await _today_buy_count(db, account.id, DAY) == 1
        assert (await paper._same_day_buy_layers(db, account_id=account.id, code="600001", now=AT))[0] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("corruption", [
    "foreign_account", "wrong_code", "wrong_side", "wrong_broker", "wrong_version",
    "wrong_quantity", "wrong_price", "wrong_clock", "wrong_round", "wrong_raw",
    "missing_order", "future_order", "duplicate_receipt", "future_fill",
    "commission", "overclaimed_quantity", "duplicate_account",
    "missing_timing", "wrong_request_id", "timing_wrong_account",
])
async def test_unverified_receipt_cannot_release_layer_or_new_name_quota(paper_client, corruption):
    _, maker = paper_client
    async with maker() as db:
        account, order, trades, fills = await seed(db)
        if corruption == "foreign_account":
            order.account_id = "default"
        elif corruption == "wrong_code":
            order.code = "600002"
        elif corruption == "wrong_side":
            fills[0].side = "sell"
        elif corruption == "wrong_broker":
            fills[0].broker = "other"
        elif corruption == "wrong_version":
            order.strategy_version = "other"
        elif corruption == "wrong_quantity":
            fills[0].quantity += 100
        elif corruption == "wrong_price":
            fills[0].price += .01
        elif corruption == "wrong_clock":
            fills[0].filled_at += timedelta(seconds=1)
        elif corruption == "wrong_round":
            fills[0].fill_round_id = "unrelated"
        elif corruption == "wrong_raw":
            fills[0].raw_json = json.dumps({"id": trades[1].id})
        elif corruption == "missing_order":
            fills[0].order_id = "absent"
        elif corruption == "future_order":
            order.created_at = AT + timedelta(seconds=1)
        elif corruption == "future_fill":
            fills[0].filled_at = AT + timedelta(seconds=1)
        elif corruption == "commission":
            fills[0].commission += 1
        elif corruption == "overclaimed_quantity":
            order.filled_quantity = 200  # each individual slice fits, their sum does not
        elif corruption == "duplicate_account":
            db.add(PaperAccount(account_name=account.account_name, initial_capital=50000))
        elif corruption == "missing_timing":
            fills[0].raw_json = json.dumps(paper._trade_payload(trades[0]))
        elif corruption == "wrong_request_id":
            fills[0].fill_id = "fill-unrelated-request"
        elif corruption == "timing_wrong_account":
            raw = json.loads(fills[0].raw_json)
            raw["pending_execution_timing"]["account_id"] = "foreign"
            fills[0].raw_json = json.dumps(raw)
        else:
            db.add(TradeFill(fill_id="duplicate", order_id=order.order_id, broker="paper",
                code=order.code, side="buy", price=10, quantity=100,
                broker_trade_id=str(trades[0].id), filled_at=trades[0].trade_time,
                trade_date=DAY, raw_json=fills[0].raw_json))
        await db.commit()
        assert (await paper._same_day_buy_layers(db, account_id=account.id, code="600001", now=AT))[0] == 2
        assert await _today_buy_count(db, account.id, DAY, as_of=AT) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("deferred", [True, False])
async def test_exact_audit_round_clock_may_precede_physical_fill(paper_client, deferred):
    _, maker = paper_client
    async with maker() as db:
        account, order, trades, fills = await seed(db, quantities=(100,))
        if not deferred:
            # Model the existing immediate receipt contract, not a deferred alias.
            order.risk_json = "{}"
            trades[0].signal_id = order.signal_id
            fills[0].fill_id = "fill-" + order.order_id
            fills[0].raw_json = json.dumps({
                **paper._trade_payload(trades[0]),
                "immediate_execution_evidence": {
                    "contract_version": "immediate_paper_fill_v2_20260914",
                    "mandatory": True, "status": "fillable",
                    "account_id": account.account_name, "code": order.code, "side": "buy",
                    "quote_round_id": trades[0].fill_round_id,
                    "fill_price": trades[0].price, "filled_quantity": trades[0].amount,
                },
            })
        db.add(PaperAutoTradeLog(account_id=account.id, code="600001", action="buy",
            decision="executed", run_id="quote-clock", created_at=order.decision_at,
            trade_date=DAY, strategy_version=trades[0].strategy_version,
            quote_round_id=order.decision_round_id, executed_trade_id=trades[0].id,
            candidate_json=json.dumps({"scale_in": True})))
        await db.commit()
        assert await _today_buy_count(db, account.id, DAY, as_of=AT) == 0


@pytest.mark.asyncio
async def test_book_clock_sees_late_slice_without_advancing_market_confirmation(paper_client):
    _, maker = paper_client
    async with maker() as db:
        account, _, trades, _ = await seed(db)
        round_start = trades[0].trade_time
        assert await paper._same_day_buy_layers(
            db, account_id=account.id, code="600001", now=round_start, as_of=AT
        ) == (1, trades[-1].trade_time)


@pytest.mark.asyncio
async def test_future_receipt_cannot_use_legacy_fallback_to_free_quota(paper_client):
    _, maker = paper_client
    async with maker() as db:
        account, _, trades, fills = await seed(db, quantities=(100,))
        fills[0].filled_at = AT + timedelta(seconds=1)
        db.add(PaperAutoTradeLog(account_id=account.id, code="600001", action="buy",
            decision="executed", run_id="contradictory-future-receipt", created_at=AT,
            trade_date=DAY, strategy_version=trades[0].strategy_version,
            executed_trade_id=trades[0].id, candidate_json=json.dumps({"scale_in": True})))
        await db.commit()
        assert await _today_buy_count(db, account.id, DAY, as_of=AT) == 1


@pytest.mark.asyncio
async def test_future_trade_is_not_consumed_and_last_fill_not_first_controls_cooldown(paper_client):
    _, maker = paper_client
    async with maker() as db:
        account, _, trades, _ = await seed(db)
        cutoff = trades[0].trade_time
        assert (await paper._same_day_buy_layers(db, account_id=account.id, code="600001", now=cutoff)) == (1, cutoff)
        db.add(PaperTradeLog(account_id=account.id, code="600002", trade_type="buy",
                            price=10, amount=100, trade_time=AT+timedelta(seconds=1)))
        await db.commit()
        assert await _today_buy_count(db, account.id, DAY, as_of=AT) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["code", "future", "version"])
async def test_mismatched_legacy_executed_trade_log_does_not_exempt_buy(paper_client, mutation):
    _, maker = paper_client
    async with maker() as db:
        account, _, trades, fills = await seed(db, quantities=(100,))
        await db.delete(fills[0])
        db.add(PaperAutoTradeLog(account_id=account.id,
            code="600002" if mutation == "code" else "600001", action="buy",
            decision="executed", run_id="legacy", created_at=AT+timedelta(seconds=1) if mutation == "future" else AT,
            trade_date=DAY, strategy_version="bad" if mutation == "version" else trades[0].strategy_version,
            executed_trade_id=trades[0].id, candidate_json=json.dumps({"scale_in": True})))
        await db.commit()
        assert await _today_buy_count(db, account.id, DAY, as_of=AT) == 1
