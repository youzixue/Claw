"""Primary pending severity: existing thresholds, isolated DB, no production I/O."""
import copy
import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.api.v1 import paper
from app.config.settings import settings
from app.models.trading import TradeFill, TradeOrder
from app.trading import service
from test_pending_buy_validity import AT, quote
from test_paper_deferred_exit_provenance import memory_session
from paper_pending_fixture import accepted_frame

ACCOUNTS = ("default", "promotion", "mainline", "auction",
            "tenbagger", "reversal", "challenger_e")


def candidate_for(account):
    source = {"default": "next_day_plan", "promotion": "promotion_promotion",
              "mainline": "promotion_mainline", "auction": "promotion_auction",
              "tenbagger": "tenbagger_midline", "challenger_e": "tenbagger_midline",
              "reversal": "reversal_pullback"}[account]
    from app.paper.experiment import HIGHBOARD_ENTRY_MODE_CONTRACT_VERSION
    return {"code": "002988", "_source": source,
            "prediction_run_key": "original", "signal_date": "2026-09-09",
            **({"entry_mode_contract": HIGHBOARD_ENTRY_MODE_CONTRACT_VERSION,
                "entry_variant": "e_low_entry" if account == "tenbagger" else "e2_strong_entry"}
               if account in {"tenbagger", "challenger_e"} else {})}


@pytest.fixture
def normal_routes(monkeypatch):
    from app.paper.experiment import HIGHBOARD_ENTRY_MODE_CONTRACT_VERSION
    row = {"prediction_run_key": "original", "signal_date": "2026-09-09",
           "entry_mode_contract": HIGHBOARD_ENTRY_MODE_CONTRACT_VERSION}
    generators = []
    for name in ("_promotion_route_buy_candidates", "_tenbagger_midline_candidates",
                 "_reversal_pullback_candidates"):
        async def highboard(*args, **kwargs):
            mode = "e_low_entry" if kwargs["account_name"] == "tenbagger" else "e2_strong_entry"
            return [{**row, "entry_variant": mode}], []
        mock = (AsyncMock(side_effect=highboard) if name == "_tenbagger_midline_candidates"
                else AsyncMock(return_value=([row], [])))
        monkeypatch.setattr(paper, name, mock)
        generators.append(mock)
    monkeypatch.setattr(paper, "_confirm_candidate_main_fund", AsyncMock(return_value=""))
    monkeypatch.setattr(paper, "_continuation_risk_reject_reason", AsyncMock(return_value=""))
    monkeypatch.setattr(paper, "_candidate_execution_value_reject_reason", lambda *a, **k: "")
    monkeypatch.setattr(paper, "_a_entry_price_band", lambda *a: {"status": "nonempty"})
    return generators


async def confirm(account, fields=None, *, db=None, candidate=None):
    candidate = candidate or candidate_for(account)
    return await paper._pending_primary_buy_confirmation(
        db, account_name=account, source=candidate["_source"], candidate=candidate,
        spot=quote(AT, **(fields or {})), limit_price=30.88, now=AT)


@pytest.mark.asyncio
@pytest.mark.parametrize("account", ACCOUNTS)
@pytest.mark.parametrize("missing", ("high", "prev_close", "limit_up", "low"))
async def test_known_below_vwap_dominates_unknown(account, missing):
    fields = {"price": 30.4, "avg_price": 30.5, missing: None}
    result = await confirm(account, fields)
    assert result[0] == "canceled"
    assert "VWAP" in result[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("account", ACCOUNTS)
@pytest.mark.parametrize("missing", ("avg_price", "prev_close", "limit_up"))
async def test_known_pullback_dominates_unknown(account, missing, monkeypatch):
    monkeypatch.setattr(settings, "PAPER_AUTO_ENTRY_ANCHOR", "legacy_high")
    result = await confirm(account, {"high": 40., missing: None})
    assert result[0] == "canceled"
    assert "回落" in result[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ("high", "low", "prev_close", "limit_up"))
async def test_a_vwap_premium_dominates_unknown(missing, monkeypatch):
    monkeypatch.setattr(settings, "PAPER_AUTO_ENTRY_ANCHOR", "vwap_reclaim")
    result = await confirm("default", {"price": 30.82, "avg_price": 20., missing: None})
    assert result[0] == "canceled"


@pytest.mark.asyncio
@pytest.mark.parametrize("account", ACCOUNTS)
@pytest.mark.parametrize("missing", ("price", "prev_close", "avg_price", "high", "limit_up"))
@pytest.mark.parametrize("unknown", (None, float("nan"), float("inf"), float("-inf"), True, False))
async def test_unknown_only_never_fills_and_f_checks_independent_identity(account, missing, unknown, normal_routes):
    result = await confirm(account, {missing: unknown})
    assert result[0] == "waiting"
    # F v1 additionally checks independent invalidation; no other route changes.
    assert [mock.await_count for mock in normal_routes] == ([0, 0, 1] if account == "reversal" else [0, 0, 0])


@pytest.mark.asyncio
@pytest.mark.parametrize("account", ACCOUNTS)
@pytest.mark.parametrize("field,value", (("high", 30.), ("high", 0.),
                                        ("prev_close", -1.), ("limit_up", 0.)))
async def test_unusable_geometry_is_unknown_not_terminal(account, field, value, normal_routes):
    assert (await confirm(account, {field: value}))[0] == "waiting"


@pytest.mark.asyncio
@pytest.mark.parametrize("account", ACCOUNTS)
@pytest.mark.parametrize("bad", (None, float("nan"), float("inf"), True))
async def test_missing_change_does_not_hide_known_vwap_failure(account, bad):
    assert (await confirm(account, {"price": 30.4, "change_pct": bad}))[0] == "canceled"


@pytest.mark.asyncio
@pytest.mark.parametrize("account", ACCOUNTS)
async def test_normal_existing_routes_and_candidate_immutable(account, normal_routes):
    candidate = candidate_for(account)
    before = copy.deepcopy(candidate)
    assert await confirm(account, candidate=candidate) == ("valid", "")
    assert candidate == before


@pytest.mark.asyncio
@pytest.mark.parametrize("account", ACCOUNTS)
async def test_identity_failure_dominates_unknown(account):
    candidate = candidate_for(account)
    candidate["code"] = "wrong"
    assert (await confirm(account, {"price": None}, candidate=candidate))[0] == "canceled"


@pytest.mark.asyncio
@pytest.mark.parametrize("account", ("promotion", "mainline", "auction"))
async def test_route_account_mismatch_dominates_missing_quote(account):
    candidate = candidate_for(account)
    candidate["_source"] = "promotion_wrong"
    assert (await confirm(account, {"high": None}, candidate=candidate))[0] == "canceled"


@pytest.mark.asyncio
@pytest.mark.parametrize("anchor", ("legacy_high", "vwap_reclaim"))
@pytest.mark.parametrize("high,avg", ((30.9, 30.5), (35., 30.5), (30.9, 31.), (30.9, 20.)))
@pytest.mark.parametrize("account", ACCOUNTS)
async def test_complete_quote_preserves_original_stable_predicates(
    account, high, avg, anchor, monkeypatch, normal_routes,
):
    monkeypatch.setattr(settings, "PAPER_AUTO_ENTRY_ANCHOR", anchor)
    spot = quote(AT, high=high, avg_price=avg)
    stable, _, _ = paper._stable_intraday_entry_quote(spot, account_name=account)
    assert (await confirm(account, {"high": high, "avg_price": avg}))[0] == (
        "valid" if stable else "canceled")


@pytest.mark.asyncio
@pytest.mark.parametrize("account", ACCOUNTS)
async def test_issue_order_and_human_wording_do_not_decide_severity(account, monkeypatch, normal_routes):
    original = paper._pending_primary_quote_issues
    def reordered(spot, **kwargs):
        return [{**issue, "reason": "统一展示文案"} for issue in reversed(original(spot, **kwargs))]
    monkeypatch.setattr(paper, "_pending_primary_quote_issues", reordered)
    assert (await confirm(account, {"high": None, "price": 30.4}))[0] == "canceled"
    assert (await confirm(account, {"high": None}))[0] == "waiting"


@pytest.mark.asyncio
@pytest.mark.parametrize("account", ACCOUNTS)
async def test_unknown_does_not_tombstone_recovered_quote(account, normal_routes):
    assert (await confirm(account, {"high": None}))[0] == "waiting"
    assert await confirm(account) == ("valid", "")


@pytest.mark.asyncio
@pytest.mark.parametrize("account", ACCOUNTS)
@pytest.mark.parametrize("threshold,expected", ((1., "canceled"), (5., "waiting")))
async def test_account_pullback_threshold_remains_independent(account, threshold, expected, monkeypatch, normal_routes):
    monkeypatch.setattr(settings, "PAPER_AUTO_ENTRY_ANCHOR", "legacy_high")
    original = paper.account_confirmation_policy
    monkeypatch.setattr(paper, "account_confirmation_policy",
        lambda name="default": {**original(name), "max_pullback_from_high_pct": threshold})
    assert (await confirm(account, {"high": 32., "prev_close": None}))[0] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", (None, "high", "prev_close", "avg_price", "limit_up"))
async def test_a_invalid_low_preserves_existing_terminal_with_or_without_unknown(missing, monkeypatch):
    monkeypatch.setattr(settings, "PAPER_AUTO_ENTRY_ANCHOR", "vwap_reclaim")
    fields = {"low": 31., "high": 31.2}
    if missing is not None:
        fields[missing] = None
    # The complete old gate already rejects low > price: missing independent
    # inputs must neither conceal that contradiction nor soften its terminal.
    if missing is None:
        assert paper._stable_intraday_entry_quote(quote(AT, **fields), account_name="default")[0] is False
    assert (await confirm("default", fields))[0] == "canceled"


@pytest.mark.asyncio
async def test_a_vwap_branch_does_not_gain_day_high_rule(monkeypatch):
    monkeypatch.setattr(settings, "PAPER_AUTO_ENTRY_ANCHOR", "vwap_reclaim")
    assert (await confirm("default", {"high": 40., "prev_close": None}))[0] == "waiting"


@pytest.mark.asyncio
async def test_partial_fill_then_mixed_invalid_cancels_only_remainder(
    memory_session, monkeypatch, normal_routes,
):
    db = memory_session
    monkeypatch.setattr(service, "experiment_active", lambda *a, **k: False)
    monkeypatch.setattr(service, "_pre_trade_risk_check", AsyncMock(return_value={
        "final_level": "pass", "block_reasons": [], "warnings": []}))
    broker = AsyncMock()
    async def fill(_db, request):
        return SimpleNamespace(accepted=True, external_order_id="fixture", fills=[
            SimpleNamespace(fill_id=f"primary-{request.order_id}", price=request.price,
                quantity=request.quantity, commission=5., tax=0., realized_pnl=0.,
                broker_trade_id="1", filled_at=request.filled_at, raw={})])
    broker.place_order.side_effect = fill
    monkeypatch.setattr(service, "get_broker_adapter", lambda _: broker)
    monkeypatch.setattr(settings, "PAPER_DEPTH_MAX_PARTICIPATION_RATIO", 1.)
    candidate = candidate_for("default")
    submitted = await service.submit_order(db, service.SubmitOrderCommand(
        code=candidate["code"], side="buy", price=30.88, quantity=300,
        account_id="default", strategy_id="paper-auto-short",
        strategy_version=paper._strategy_version("default"), source=candidate["_source"],
        signal_id="primary-isolated", decision_at=AT, as_of_at=AT, decision_round_id="decision",
        defer_until_next_round=True, deferred_metadata={
            "candidate": candidate, "confirmed_at": AT.isoformat(), "block_warn": True}))
    assert submitted["order"]["status"] == "submitted"
    order = await db.scalar(select(TradeOrder))
    async def run(at, round_id, **fields):
        spot = quote(at, quote_round_id=round_id, **fields)
        monkeypatch.setattr(service, "_paper_execution_spot", AsyncMock(return_value=spot))
        payload = {"round_id": round_id, "quality_status": "ok", "committed_at": at,
                   "as_of_at": at, "config_version": "primary-test", "code_version": "primary-test",
                   "records": [vars(spot)]}
        await accepted_frame(db, payload)
        monkeypatch.setattr(paper, "_public_order_clock", lambda: at)
        token = paper._QUOTE_ROUND_CONTEXT.set(payload)
        try:
            return await service.reconcile_paper_deferred_orders(
                db, account_id="default", now=at, round_id=round_id)
        finally:
            paper._QUOTE_ROUND_CONTEXT.reset(token)
    first = await run(AT + timedelta(seconds=60), "fill-one", ask1_volume=1)
    assert first[0]["event"] == "partial" and order.filled_quantity == 100
    old_fill = await db.scalar(select(TradeFill))
    before = (old_fill.price, old_fill.quantity, old_fill.filled_at, old_fill.commission)
    frozen = copy.deepcopy(json.loads(order.risk_json)["paper_deferred_order"]["buy_validity"])
    waiting = await run(AT + timedelta(seconds=75), "fill-unknown", high=None)
    assert waiting[0]["event"] == "waiting" and order.status == "partial"
    assert order.filled_quantity == 100 and broker.place_order.await_count == 1
    second = await run(AT + timedelta(seconds=90), "fill-two", price=30.4, high=None)
    assert second[0]["event"] == "canceled"
    assert order.status == "canceled" and order.filled_quantity == 100
    assert (old_fill.price, old_fill.quantity, old_fill.filled_at, old_fill.commission) == before
    assert len(list((await db.scalars(select(TradeFill))).all())) == 1
    assert json.loads(order.risk_json)["paper_deferred_order"]["buy_validity"] == frozen
    assert broker.place_order.await_count == 1
    assert await run(AT + timedelta(seconds=120), "fill-three") == []


@pytest.mark.asyncio
@pytest.mark.parametrize("account", ("tenbagger", "challenger_e"))
@pytest.mark.parametrize("missing", ("high", "prev_close", "limit_up"))
async def test_real_limit_queue_unknown_then_mixed_invalid_never_fills(
    memory_session, monkeypatch, account, missing,
):
    """Real submit/freeze/reconcile/cancel; no mock of primary or validity reducer."""
    db = memory_session
    at = AT + timedelta(minutes=30)
    monkeypatch.setattr(service, "experiment_active", lambda *a, **k: False)
    risk = AsyncMock(return_value={"final_level": "pass", "block_reasons": [], "warnings": []})
    broker = AsyncMock()
    monkeypatch.setattr(service, "_pre_trade_risk_check", risk)
    monkeypatch.setattr(service, "get_broker_adapter", lambda _: broker)
    spot = quote(at, code="600001", price=11., prev_close=10., high=11.,
        low=10.4, open=10.5, avg_price=10.7, change_pct=10., limit_up=11., limit_down=9.,
        bid1_price=11., bid1_volume=100, ask1_price=0., ask1_volume=0.,
        volume=50000, quote_round_id="primary-queue-decision")
    monkeypatch.setattr(service, "_paper_execution_spot", AsyncMock(side_effect=lambda *_: spot))
    from app.paper.experiment import HIGHBOARD_ENTRY_MODE_CONTRACT_VERSION
    candidate = {"code": "600001", "_source": "tenbagger_midline",
                 "entry_mode_contract": HIGHBOARD_ENTRY_MODE_CONTRACT_VERSION,
                 "entry_variant": "e_low_entry" if account == "tenbagger" else "e2_limit_touch",
                 "signal_date": (AT.date()-timedelta(days=1)).isoformat()}
    submitted = await service.submit_order(db, service.SubmitOrderCommand(
        code="600001", side="buy", quantity=100, price=11.,
        account_id=account, strategy_id="paper-auto-short", source="tenbagger_midline",
        strategy_version=paper._strategy_version(account), signal_id="auto-primary-queue-test",
        decision_at=at, as_of_at=at, decision_round_id="primary-queue-decision",
        queue_if_limit_up=True, queue_metadata={"candidate": candidate,
            "confirmed_at": at.isoformat(), "cancel_time": "14:50"}))
    assert submitted["order"]["status"] == "submitted"
    order = await db.scalar(select(TradeOrder))
    frozen = copy.deepcopy(json.loads(order.risk_json)["paper_limit_up_queue"]["buy_validity"])
    primary = AsyncMock(wraps=paper._pending_primary_buy_confirmation)
    monkeypatch.setattr(paper, "_pending_primary_buy_confirmation", primary)
    async def run(seconds, round_id):
        now = at + timedelta(seconds=seconds)
        spot.updated_at = spot.received_at = spot.source_quote_at = now
        spot.quote_round_id = round_id
        # A healthy decision frame; sufficient tape volume cannot override an
        # unknown/invalid original signal before the queue's fillability gate.
        spot.volume = 50101
        token = paper._QUOTE_ROUND_CONTEXT.set({
            "round_id": round_id, "quality_status": "ok", "as_of_at": now, "committed_at": now})
        try:
            return await service.reconcile_paper_limit_up_orders(db, account_id=account, now=now)
        finally:
            paper._QUOTE_ROUND_CONTEXT.reset(token)
    setattr(spot, missing, None)
    waiting = await run(30, "primary-queue-unknown")
    assert waiting[0]["event"] == "waiting" and order.status == "submitted"
    assert primary.await_count == 1
    spot.price = 10.6  # Valid price and VWAP: known broken VWAP plus independent unknown.
    canceled = await run(60, "primary-queue-mixed")
    assert canceled[0]["event"] == "canceled" and "VWAP" in canceled[0]["reason"]
    assert primary.await_count == 2
    assert order.status == "canceled" and order.filled_quantity == 0
    saved = json.loads(order.risk_json)["paper_limit_up_queue"]
    assert saved["candidate"] == candidate and saved["buy_validity"] == frozen
    assert saved["buy_validity_evaluation"]["remaining_quantity"] == 100
    assert list((await db.scalars(select(TradeFill))).all()) == []
    assert broker.place_order.await_count == 0 and risk.await_count == 1
    spot.price, spot.high, spot.prev_close, spot.limit_up = 11., 11., 10., 11.
    assert await run(90, "primary-queue-recovered") == []
    assert primary.await_count == 2 and broker.place_order.await_count == 0
