"""Dormant partial CAS tests: fabricated future scope and real TEMPORARY DB rows.

NO partial broker/book/risk integration is claimed. Private future scope fields
are set only by this fixture; production authorization cannot issue them.
"""
import asyncio
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import json

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import select, update, func, text, event
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.api.v1 import paper
from app.data.after_hours import _json
from app.db.session import Base
from app.models.paper import PaperAccount, PaperTradeLog, PaperPosition
from app.models.trading import TradeOrder, TradeFill, PaperAfterHoursResourceScope as State, PaperAfterHoursResourceReceipt as Receipt
from app.trading import paper_after_hours_resources as resources, paper_after_hours_execution as execution
from app.trading import paper_after_hours_allocation as allocation, paper_authorization as auth
from test_paper_after_hours_allocation_20261002 import AT, NOW, SOURCE, audited_fixture
from test_paper_after_hours_partial_contract_20261002 import original, freeze, request
from test_paper_after_hours_partial_allocation_20261002 import capacity, append_offer, simulated_state
from test_paper_after_hours_partial_book_20261002 import book, BOOK_FIELDS, second_and_third
from test_paper_after_hours_partial_fees_20261002 import fee_model

ORDER_FIELDS = ("id", "order_id", "order_type", "broker", "account_id", "code", "side", "price", "quantity",
    "status", "filled_quantity", "trade_date", "risk_json", "strategy_version", "signal_id", "decision_round_id",
    "created_at", "decision_at")
FILL_FIELDS = ("id", "fill_id", "order_id", "broker", "broker_trade_id", "code", "side", "price", "quantity",
    "commission", "tax", "realized_pnl", "decision_round_id", "fill_round_id", "trade_date", "filled_at", "raw_json")


@pytest_asyncio.fixture
async def database(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'partial-resources.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(text("PRAGMA foreign_keys=OFF"))
        await conn.execute(text("PRAGMA recursive_triggers=OFF"))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as db:
        db.add(PaperAccount(id=7, account_name="default", strategy="default", status="active",
            initial_capital=50000, current_capital=50000, total_assets=50000))
        await db.commit()
    monkeypatch.setattr(paper, "_public_order_clock", lambda: NOW)
    try:
        yield maker, engine
    finally:
        await engine.dispose()


@contextmanager
def future_scope(db, req, candidate, contract, timing):
    """Fixture-only future ledger state; the normal scope rejects this candidate."""
    token = auth._PAPER_TRANSACTION.set((db, asyncio.current_task()))
    try:
        with auth._paper_execution_scope(db, req, immediate_evidence_json=_json(contract)):
            scope = auth._SCOPE.get()
            scope.fixed_candidate = candidate
            scope.stage, scope.ledger_timing = "ledger", timing
            yield scope
    finally:
        auth._PAPER_TRANSACTION.reset(token)


async def stage(db, item):
    candidate, req, view, fill_view, trade_view, _, pure_timing = item
    order = await db.scalar(select(TradeOrder).where(TradeOrder.order_id == view.order_id))
    if order is None:
        order = TradeOrder(**{key: getattr(view, key) for key in ORDER_FIELDS})
        db.add(order)
    await db.flush()
    contract = resources._partial_resource_contract(candidate)
    timing = resources._partial_resource_timing(candidate, pure_timing)
    trade = PaperTradeLog(**{key: getattr(trade_view, key) for key in
        (*BOOK_FIELDS, "account_id", "trade_time")})
    raw = json.loads(fill_view.raw_json)
    raw["after_hours_fixed_execution"], raw["ledger_execution_timing"] = contract, timing
    fill = TradeFill(**{key: getattr(fill_view, key) for key in FILL_FIELDS if key != "raw_json"},
                     raw_json=_json(raw))
    db.add_all([trade, fill])
    # Explicit synthetic economic mutations only to observe whole transaction rollback.
    account = await db.get(PaperAccount, 7)
    account.current_capital -= fill.price * fill.quantity + fill.commission + fill.tax
    position = await db.scalar(select(PaperPosition).where(PaperPosition.account_id == 7))
    if position is None:
        db.add(PaperPosition(account_id=7, code=order.code, buy_price=fill.price, buy_amount=fill.quantity,
                            buy_time=fill.filled_at, is_closed=False))
    else:
        position.buy_amount += fill.quantity
    await db.flush()
    return order, fill, trade, contract, timing, req


async def persist(db, item, *, mutate=None, complete=True):
    candidate = item[0]
    order, fill, trade, contract, timing, req = await stage(db, item)
    if mutate:
        mutate(order, fill, trade, contract, timing, req)
    with future_scope(db, req, candidate, contract, timing) as scope:
        out = await resources._persist_partial_fill_resources(db, candidate=candidate,
            order_id=order.order_id, fill_id=fill.fill_id, feed=json.loads(candidate.feed_json))
        assert scope.resource_consumption_used is True
        if complete:
            # Future caller-owned order CAS, after actual book -> resource -> receipt.
            changed = await db.execute(update(TradeOrder).where(TradeOrder.order_id == order.order_id,
                TradeOrder.filled_quantity == contract["cumulative_before"],
                TradeOrder.status.in_(["submitted", "partial"])).values(
                filled_quantity=contract["cumulative_after"],
                status="filled" if contract["remaining_quantity_after"] == 0 else "partial",
                avg_fill_price=contract["fill_price"], last_fill_round_id=contract["quote_round_id"]))
            assert changed.rowcount == 1
        return out


@pytest.mark.asyncio
async def test_three_fragments_persist_same_root_with_actual_distinct_book(database, audited_fixture, fee_model, monkeypatch):
    maker, _ = database
    items = second_and_third()
    for index, item in enumerate(items, 1):
        monkeypatch.setattr(paper, "_public_order_clock", lambda: item[4].trade_time)
        async with maker() as db, db.begin():
            out = await persist(db, item)
            assert out["revision"] == index and out["execution_authorized"] is False
    async with maker() as db:
        state = await db.scalar(select(State))
        account = await db.get(PaperAccount, 7)
        priors = await resources._durable_partial_priors(db, state.scope_key, account, cutoff=AT+timedelta(seconds=6))
        assert len(priors) == 3 and sum(p["quantity"] for p in priors) == 300
        assert [p["fragment_index"] for p in priors] == [1, 2, 3]
        assert (await db.scalar(select(TradeOrder))).filled_quantity == 300
        assert await db.scalar(select(func.count()).select_from(State)) == 1
        assert await db.scalar(select(func.count()).select_from(Receipt)) == 3
        assert account.current_capital == 46985
        assert (await db.scalar(select(PaperPosition))).buy_amount == 300
        with pytest.raises(ValueError):
            await resources._durable_priors(db, state.scope_key, account, cutoff=AT+timedelta(seconds=6))


@pytest.mark.asyncio
async def test_normal_authorization_still_cannot_issue_partial_scope(database, audited_fixture, fee_model):
    maker, _ = database
    c = freeze()
    async with maker() as db:
        await db.execute(select(PaperAccount.id))
        with pytest.raises(HTTPException) as exc:
            with auth._paper_execution_scope(db, request(c), immediate_evidence_json=c.contract_json, fixed_candidate=c):
                pass
        assert exc.value.status_code == 403
        with pytest.raises(HTTPException):
            await resources._persist_partial_fill_resources(db, candidate=c, order_id="head",
                fill_id="missing", feed=json.loads(c.feed_json))


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["candidate_contract", "candidate_clock", "absent_candidate", "changed_quantity", "used_scope"])
async def test_scope_candidate_or_retyped_json_is_not_authority(database, audited_fixture, fee_model, damage):
    maker, _ = database
    item = book(freeze())
    async with maker() as db:
        async with db.begin():
            order, fill, trade, contract, timing, req = await stage(db, item)
            if damage == "candidate_contract":
                contract = json.loads(item[0].contract_json)
            if damage == "candidate_clock":
                timing = item[6]
            with future_scope(db, req, item[0], contract, timing) as scope:
                if damage == "absent_candidate":
                    scope.fixed_candidate = None
                if damage == "changed_quantity":
                    req.quantity += 100
                if damage == "used_scope":
                    scope.resource_consumption_used = True
                with pytest.raises(HTTPException):
                    await resources._persist_partial_fill_resources(db, candidate=item[0],
                        order_id=order.order_id, fill_id=fill.fill_id, feed=json.loads(item[0].feed_json))
            await db.rollback()
    async with maker() as db:
        assert await db.scalar(select(func.count()).select_from(Receipt)) == 0


@pytest.mark.asyncio
async def test_fee_policy_change_during_receipt_flush_blocks_terminal_commit(database, audited_fixture, fee_model, monkeypatch):
    maker, _ = database
    item = book(freeze())
    async with maker() as db:
        def drift(session, *_):
            if any(isinstance(obj, Receipt) for obj in session.new):
                monkeypatch.setattr(paper.settings, "PAPER_MIN_COMMISSION", 6.0)
        event.listen(db.sync_session, "before_flush", drift)
        with pytest.raises(HTTPException, match="fee_model_changed"):
            async with db.begin():
                await persist(db, item)
    async with maker() as db:
        assert await db.scalar(select(func.count()).select_from(Receipt)) == 0
        assert await db.scalar(select(func.count()).select_from(TradeFill)) == 0


@pytest.mark.asyncio
async def test_final_clock_is_resampled_after_expensive_replay(database, audited_fixture, fee_model, monkeypatch):
    maker, _ = database
    item = book(freeze())
    times = iter([NOW, NOW, AT+timedelta(seconds=15)])
    monkeypatch.setattr(paper, "_public_order_clock", lambda: next(times))
    async with maker() as db:
        with pytest.raises(HTTPException, match="terminal_expiry"):
            async with db.begin():
                await persist(db, item)


@pytest.mark.parametrize("change", ["feed_refresh", "scope_clock", "scope_contract", "scope_candidate", "scope_request"])
@pytest.mark.asyncio
async def test_await_cannot_refresh_frozen_feed_or_replace_scope(database, audited_fixture, fee_model, monkeypatch, change):
    maker, _ = database
    item = book(freeze())
    shared = json.loads(item[0].feed_json)
    if change == "feed_refresh":
        times = iter([NOW, AT+timedelta(seconds=21), AT+timedelta(seconds=21)])
        monkeypatch.setattr(paper, "_public_order_clock", lambda: next(times))
    async with maker() as db:
        with pytest.raises(HTTPException):
            async with db.begin():
                order, fill, trade, contract, timing, req = await stage(db, item)
                with future_scope(db, req, item[0], contract, timing) as scope:
                    def change_after_book(session, *_):
                        if not any(isinstance(obj, Receipt) for obj in session.new):
                            return
                        if change == "feed_refresh":
                            shared.update(frame_id="refreshed", source_quote_at=(AT+timedelta(seconds=20)).isoformat(),
                                received_at=(AT+timedelta(seconds=20,milliseconds=100)).isoformat(),
                                available_at=(AT+timedelta(seconds=20,milliseconds=200)).isoformat())
                        elif change == "scope_clock":
                            scope.ledger_timing["fragment_index"] += 1
                        elif change == "scope_contract":
                            scope.immediate_evidence_json = item[0].contract_json
                        elif change == "scope_candidate":
                            scope.fixed_candidate = replace(item[0], fingerprint="0"*64)
                        else:
                            req.quantity += 100
                    event.listen(db.sync_session, "before_flush", change_after_book)
                    await resources._persist_partial_fill_resources(db, candidate=item[0],
                        order_id=order.order_id, fill_id=fill.fill_id, feed=shared)
    async with maker() as db:
        assert await db.scalar(select(func.count()).select_from(Receipt)) == 0
        assert await db.scalar(select(func.count()).select_from(TradeFill)) == 0


@pytest.mark.asyncio
async def test_original_scope_epoch_cannot_survive_rollback_and_autobegin(database, audited_fixture, fee_model):
    maker, _ = database
    item = book(freeze())
    async with maker() as db:
        order, fill, trade, contract, timing, req = await stage(db, item)
        order_id, fill_id = order.order_id, fill.fill_id
        with future_scope(db, req, item[0], contract, timing):
            await db.rollback()
            await db.execute(select(PaperAccount.id))  # Different physical root.
            with pytest.raises(HTTPException) as exc:
                await resources._persist_partial_fill_resources(db, candidate=item[0],
                    order_id=order_id, fill_id=fill_id, feed=json.loads(item[0].feed_json))
            assert exc.value.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("peer_state", ["submitted", "filled", "canceled", "unreceipted_fill"])
async def test_all_original_peers_cannot_be_omitted(database, audited_fixture, fee_model, peer_state):
    maker, _ = database
    item = book(freeze())
    async with maker() as db:
        with pytest.raises(HTTPException):
            async with db.begin():
                peer = original("peer", quantity=100, sequence=2)
                if peer_state == "filled":
                    peer.status, peer.filled_quantity = "filled", 100
                elif peer_state == "canceled":
                    peer.status = "canceled"
                db.add(TradeOrder(**{key: getattr(peer, key) for key in ORDER_FIELDS}))
                if peer_state == "unreceipted_fill":
                    db.add(TradeFill(id=99, fill_id="old.orphan", order_id="peer", broker="paper", code=peer.code,
                        side="buy", quantity=100, price=10, trade_date=peer.trade_date, filled_at=NOW, raw_json="{}"))
                await persist(db, item)
    async with maker() as db:
        assert await db.scalar(select(func.count()).select_from(TradeFill)) == 0
        assert await db.scalar(select(func.count()).select_from(State)) == 0
        assert (await db.get(PaperAccount, 7)).current_capital == 50000


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["late_expiry", "late_rollback", "receipt_flush", "order_cas"])
async def test_late_failure_rolls_back_entire_synthetic_book(database, audited_fixture, fee_model, monkeypatch, failure):
    maker, _ = database
    item = book(freeze())
    if failure in {"late_expiry", "late_rollback"}:
        times = iter([NOW, AT+timedelta(seconds=15) if failure == "late_expiry" else AT])
        monkeypatch.setattr(paper, "_public_order_clock", lambda: next(times))
    async with maker() as db:
        if failure == "receipt_flush":
            def fail(session, *_):
                if any(isinstance(obj, Receipt) for obj in session.new):
                    raise RuntimeError("fixture receipt failure")
            event.listen(db.sync_session, "before_flush", fail)
        with pytest.raises((HTTPException, RuntimeError)):
            async with db.begin():
                await persist(db, item)
                if failure == "order_cas":
                    raise RuntimeError("fixture caller CAS failure")
    async with maker() as db:
        for model in (TradeOrder, TradeFill, PaperTradeLog, PaperPosition, Receipt, State):
            assert await db.scalar(select(func.count()).select_from(model)) == 0
        assert (await db.get(PaperAccount, 7)).current_capital == 50000


@pytest.mark.asyncio
async def test_durable_fee_history_uses_frozen_parameters_not_current_settings(database, audited_fixture, fee_model, monkeypatch):
    maker, _ = database
    item = book(freeze())
    async with maker() as db, db.begin():
        await persist(db, item)
    monkeypatch.setattr(paper.settings, "PAPER_MIN_COMMISSION", 9.0)
    def no_live_policy():
        raise RuntimeError("must not read live fee policy for history")
    monkeypatch.setattr(execution, "_partial_fee_parameters", no_live_policy)
    async with maker() as db:
        state = await db.scalar(select(State))
        priors = await resources._durable_partial_priors(db, state.scope_key, await db.get(PaperAccount, 7), cutoff=NOW)
        assert len(priors) == 1 and priors[0]["fee_model"]["minimum_commission"] == 5.0


@pytest.mark.parametrize("change", [
    lambda m: m.update(minimum_commission=True),
    lambda m: m.update(minimum_commission=5),
    lambda m: m.update(commission_rate=-1.0),
    lambda m: m.update(stamp_tax_rate=1.0),
    lambda m: m.update(model_version="unknown"),
    lambda m: m.update(unrecognized=True),
])
def test_frozen_fee_history_model_is_exact_and_typed(audited_fixture, fee_model, change):
    proposal = json.loads(freeze().contract_json)["allocation"]
    model = deepcopy(proposal["fee_model"])
    change(model)
    with pytest.raises(ValueError):
        execution._partial_fee_preview(proposal, frozen_model=model)


@pytest.mark.asyncio
async def test_current_fee_drift_still_blocks_new_finalize(database, audited_fixture, fee_model, monkeypatch):
    maker, _ = database
    item = book(freeze())
    monkeypatch.setattr(paper.settings, "PAPER_MIN_COMMISSION", 6.0)
    async with maker() as db:
        with pytest.raises((HTTPException, ValueError)):
            async with db.begin():
                await persist(db, item)
    async with maker() as db:
        assert await db.scalar(select(func.count()).select_from(Receipt)) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["projection", "current_raw", "duplicate_raw_key", "current_protocol"])
async def test_current_actual_rows_must_match_before_coordinate_and_typed_contract(database, audited_fixture, fee_model, damage):
    maker, _ = database
    item = book(freeze())
    async with maker() as db:
        with pytest.raises(HTTPException):
            async with db.begin():
                order, fill, trade, contract, timing, req = await stage(db, item)
                if damage == "projection":
                    order.filled_quantity = contract["cumulative_after"]
                elif damage == "current_raw":
                    raw = json.loads(fill.raw_json)
                    raw["ledger_execution_timing"]["fragment_index"] = 2
                    fill.raw_json = _json(raw)
                elif damage == "duplicate_raw_key":
                    fill.raw_json = '{"id":101,' + fill.raw_json[1:]
                else:
                    raw = json.loads(fill.raw_json)
                    raw["after_hours_fixed_execution"] = json.loads(item[0].contract_json)
                    fill.raw_json = _json(raw)
                with future_scope(db, req, item[0], contract, timing):
                    await resources._persist_partial_fill_resources(db, candidate=item[0], order_id=order.order_id,
                        fill_id=fill.fill_id, feed=json.loads(item[0].feed_json))


@pytest.mark.asyncio
async def test_complete_filled_peer_and_global_receipts_remain_in_context(database, audited_fixture, fee_model, monkeypatch):
    maker, _ = database
    head = original(quantity=100)
    later = original("later", quantity=100, sequence=2)
    payload = capacity(200)
    first = book(freeze(payload, head, context=[head, later]))
    prior = json.loads(first[0].contract_json)["allocation"]
    second = book(freeze(payload, later, context=[simulated_state(head, 100), later],
        priors=[prior]), sequence=2)
    async with maker() as db, db.begin():
        db.add(TradeOrder(**{key: getattr(later, key) for key in ORDER_FIELDS}))
        await persist(db, first)
    async with maker() as db, db.begin():
        out = await persist(db, second)
        assert out["revision"] == 2
    async with maker() as db:
        assert (await db.scalar(select(State))).revision == 2
        assert [o.filled_quantity for o in (await db.execute(select(TradeOrder).order_by(TradeOrder.id))).scalars()] == [100, 100]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["source_version", "session_id", "frame_backward", "omitted_prior"])
async def test_next_fragment_cannot_reset_root_or_omit_consumption(database, audited_fixture, fee_model, monkeypatch, change):
    maker, _ = database
    first, second, _ = second_and_third()
    async with maker() as db, db.begin():
        await persist(db, first)
    candidate = second[0]
    payload = json.loads(candidate.feed_json)
    if change == "source_version":
        payload["source_version"] = "other.v1"
        monkeypatch.setattr(allocation, "_PROVIDERS", {
            SOURCE: allocation._Provider("other.v1", "order_level", allocation.METHOD)})
    elif change == "session_id":
        payload["session_id"] = "new.same.day"
    elif change == "frame_backward":
        payload = json.loads(first[0].feed_json)
    if change != "omitted_prior":
        # Keep actual original/complete history; each alternate source is independently
        # valid as a pure candidate, but cannot reset the stored same-account/day root.
        try:
            c = freeze(payload, simulated_state(original(), 100), dispatch=AT+timedelta(seconds=3),
                priors=json.loads(candidate.priors_json))
            changed = book(c, sequence=2)
        except ValueError:
            changed = second
            changed_payload = payload
        else:
            changed_payload = payload
    else:
        changed, changed_payload = second, json.loads(second[0].feed_json)
    monkeypatch.setattr(paper, "_public_order_clock", lambda: AT+timedelta(seconds=3))
    async with maker() as db:
        with pytest.raises(HTTPException):
            async with db.begin():
                order, fill, trade, contract, timing, req = await stage(db, changed)
                if change == "omitted_prior":
                    # Damaged original projection cannot suppress actual old receipts.
                    order.filled_quantity = 0
                with future_scope(db, req, changed[0], contract, timing):
                    await resources._persist_partial_fill_resources(db, candidate=changed[0],
                        order_id=order.order_id, fill_id=fill.fill_id, feed=changed_payload)
    async with maker() as db:
        assert await db.scalar(select(func.count()).select_from(Receipt)) == 1
        assert (await db.scalar(select(State))).revision == 1
        assert (await db.scalar(select(TradeOrder))).filled_quantity == 100


@pytest.mark.asyncio
async def test_partial_finalize_total_byte_budget_includes_all_book_and_original_reads(database, audited_fixture, fee_model, monkeypatch):
    maker, _ = database
    item = book(freeze())
    monkeypatch.setattr(resources, "MAX_READ_BYTES", 1000)
    async with maker() as db:
        with pytest.raises(HTTPException, match="byte_budget"):
            async with db.begin():
                await persist(db, item)


@pytest.mark.asyncio
async def test_same_scope_cannot_consume_twice_even_before_order_cas(database, audited_fixture, fee_model):
    maker, _ = database
    item = book(freeze())
    async with maker() as db:
        async with db.begin():
            order, fill, trade, contract, timing, req = await stage(db, item)
            with future_scope(db, req, item[0], contract, timing):
                kwargs = dict(candidate=item[0], order_id=order.order_id, fill_id=fill.fill_id,
                              feed=json.loads(item[0].feed_json))
                await resources._persist_partial_fill_resources(db, **kwargs)
                with pytest.raises(HTTPException) as exc:
                    await resources._persist_partial_fill_resources(db, **kwargs)
                assert exc.value.status_code == 403
            await db.rollback()
    async with maker() as db:
        assert await db.scalar(select(func.count()).select_from(Receipt)) == 0
