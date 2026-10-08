"""E/E2 entry-mode contracts: isolated fixtures, no live DB, broker or network."""
import copy
import json
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.api.v1 import paper
from app.models.paper import PaperAutoTradeLog
from app.models.stock import LimitUpPool, StockSpot
from app.paper import experiment
from app.paper.account_policy import ACCOUNT_NAMES
from test_paper_api import paper_client
from test_paper_deferred_exit_provenance import memory_session

DAY = date(2026, 9, 28)
PREVIOUS = date(2026, 9, 24)
AT = datetime(2026, 9, 28, 10)
ACCOUNTS = ("tenbagger", "challenger_e")


def quote(**overrides):
    return SimpleNamespace(**{
        **dict(code="001234", name="isolated", price=10.1, prev_close=10., open=10.,
               high=10.12, low=9.98, avg_price=10.05, limit_up=11., limit_down=9.,
               change_pct=1., ask1_price=10.11, ask1_volume=100,
               bid1_price=10.09, bid1_volume=100, source_quote_at=AT,
               received_at=AT, updated_at=AT, quote_round_id="isolated"),
        **overrides,
    })


def frozen(account):
    return {"code": "001234", "_source": "tenbagger_midline",
            "signal_date": PREVIOUS.isoformat(),
            "entry_mode_contract": experiment.HIGHBOARD_ENTRY_MODE_CONTRACT_VERSION,
            "entry_variant": "e2_strong_entry" if account == "challenger_e" else "e_low_entry"}


@pytest.mark.parametrize("account", ACCOUNTS)
@pytest.mark.parametrize("missing", (None, "prev_close", "open", "high", "low"))
def test_locked_down_is_terminal_even_with_other_unknowns(account, missing):
    fields = dict(price=31.86, prev_close=35.4, open=31.86, high=31.86, low=31.86,
                  avg_price=31.86, limit_down=31.86, limit_up=38.94, change_pct=-10.)
    if missing:
        fields[missing] = None
    evidence = paper._highboard_entry_evidence(quote(**fields), account_name=account)
    assert not evidence["valid"] and evidence["mode"] is None
    assert any(x["code"] == "highboard_at_limit_down" and not x["recoverable"]
               for x in evidence["issues"])


@pytest.mark.parametrize("account", ACCOUNTS)
@pytest.mark.parametrize("key", ("price", "prev_close", "open", "high", "low", "limit_up", "limit_down"))
@pytest.mark.parametrize("value", (None, 0., -1., True, False, float("nan"), float("inf"), float("-inf")))
def test_missing_nonfinite_or_bool_mode_inputs_never_pass(account, key, value):
    evidence = paper._highboard_entry_evidence(quote(**{key: value}), account_name=account)
    assert evidence["valid"] is False
    assert any(x["code"] == f"highboard_missing_{key}" for x in evidence["issues"])


@pytest.mark.parametrize("account", ACCOUNTS)
@pytest.mark.parametrize("price", (9., 10., 11.))
def test_single_price_cannot_claim_observed_intraday_path(account, price):
    e = paper._highboard_entry_evidence(
        quote(price=price, open=price, high=price, low=price, avg_price=price),
        account_name=account)
    assert not e["valid"]
    assert any(x["code"] == "highboard_no_price_path" for x in e["issues"])


@pytest.mark.parametrize("field,value", (("high", 10.), ("low", 10.11),
                                         ("open", 10.5), ("limit_down", 11.),
                                         ("prev_close", 12.)))
def test_inconsistent_geometry_is_not_a_valid_mode(field, value):
    e = paper._highboard_entry_evidence(quote(**{field: value}), account_name="tenbagger")
    assert not e["valid"]


def test_e_negative_recovery_is_not_blanket_banned_but_e2_requires_reclaim():
    q = quote(price=9.6, open=9.5, high=9.7, low=9.4, avg_price=9.55, change_pct=-4.)
    main = paper._highboard_entry_evidence(q, account_name="tenbagger")
    secondary = paper._highboard_entry_evidence(q, account_name="challenger_e")
    assert main["valid"] and main["mode"] == "e_low_recovery"
    assert not secondary["valid"]
    assert any(x["code"] == "highboard_strength_not_reclaimed" for x in secondary["issues"])
    assert paper._highboard_entry_evidence(quote(price=9.4, low=9.4, high=9.7,
        open=9.5, avg_price=9.4), account_name="tenbagger")["valid"] is False


@pytest.mark.parametrize("account,fields,mode", [
    ("tenbagger", {}, "e_low_entry"),
    ("challenger_e", {}, "e2_strong_entry"),
    ("challenger_e", {"open": 9.8, "low": 9.7}, "e2_strong_recovery"),
    ("challenger_e", {"price": 11., "high": 11.}, "e2_limit_touch"),
])
def test_modes_are_price_evidence_not_fabricated_reseal(account, fields, mode):
    q = quote(**fields)
    before = copy.deepcopy(vars(q))
    e = paper._highboard_entry_evidence(q, account_name=account)
    assert e["valid"] and e["mode"] == mode
    assert e["reseal_path_status"] == "unverified"
    assert vars(q) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("account", ACCOUNTS)
async def test_real_candidate_generator_rejects_taimushi_degenerate_shape(
    paper_client, monkeypatch, account,
):
    _, maker = paper_client
    monkeypatch.setattr(paper.trade_calendar, "previous_trade_day", AsyncMock(return_value=PREVIOUS))
    monkeypatch.setattr(paper.settings, "PAPER_TENBAGGER_ENABLED", True)
    async with maker() as db:
        db.add_all([
            LimitUpPool(code="001234", name="isolated", trade_date=PREVIOUS,
                        consecutive_days=4, seal_amount=360_000_000, break_count=0, quarantined=False),
            StockSpot(**vars(quote(price=31.86, prev_close=35.4, open=31.86, high=31.86,
                                  low=31.86, avg_price=31.86, limit_down=31.86,
                                  limit_up=38.94, change_pct=-10.))),
        ])
        await db.flush()
        diagnostics = []
        rows, _ = await paper._tenbagger_midline_candidates(
            db, limit=10, trade_date=DAY, account_name=account, diagnostics=diagnostics)
        assert rows == []
        assert diagnostics[0]["reason_code"] == "highboard_at_limit_down"
        assert diagnostics[0]["stage_code"] == "strategy_filter"
        assert diagnostics[0]["candidate"]["recoverable"] is False
        evidence = diagnostics[0]["candidate"]["quality_checks"]["entry_mode_evidence"]
        assert evidence["valid"] is False and evidence["quote"]["price"] == 31.86


@pytest.mark.asyncio
@pytest.mark.parametrize("queue", (False, True))
async def test_real_e2_candidate_freezes_actual_mode_and_never_claims_reseal(
    paper_client, monkeypatch, queue,
):
    _, maker = paper_client
    monkeypatch.setattr(paper.trade_calendar, "previous_trade_day", AsyncMock(return_value=PREVIOUS))
    monkeypatch.setattr(paper.settings, "PAPER_TENBAGGER_ENABLED", True)
    monkeypatch.setattr(paper.settings, "PAPER_CHALLENGER_E_LIMIT_UP_QUEUE_ENABLED", True)
    fields = (dict(price=11., high=11., avg_price=10.7, change_pct=10.,
                   ask1_price=0., ask1_volume=0, bid1_price=11., bid1_volume=100)
              if queue else {})
    async with maker() as db:
        db.add_all([LimitUpPool(code="001234", trade_date=PREVIOUS, consecutive_days=4,
                               seal_amount=360_000_000, break_count=0, quarantined=False),
                    StockSpot(**vars(quote(**fields)))])
        await db.flush()
        rows, _ = await paper._tenbagger_midline_candidates(
            db, limit=10, trade_date=DAY, account_name="challenger_e")
        assert len(rows) == 1
        c = rows[0]
        assert c["entry_variant"] == ("e2_limit_touch" if queue else "e2_strong_entry")
        assert c["entry_mode_contract"] == experiment.HIGHBOARD_ENTRY_MODE_CONTRACT_VERSION
        assert c["entry_mode_evidence"]["reseal_path_status"] == "unverified"
        assert "未认证回封时序" in c["entry_condition"]
        assert c["limit_up_queue"] is queue


@pytest.mark.asyncio
@pytest.mark.parametrize("account", ACCOUNTS)
@pytest.mark.parametrize("field", ("avg_price", "prev_close", "high", "low"))
async def test_pending_known_limit_down_dominates_unknown(account, field):
    q = quote(**{**dict(price=9., open=9., high=9., low=9., avg_price=9., change_pct=-10.),
                 field: None})
    status, reason = await paper._pending_primary_buy_confirmation(
        None, account_name=account, source="tenbagger_midline", candidate=frozen(account),
        spot=q, limit_price=9., now=AT)
    assert status == "canceled" and "跌停" in reason


@pytest.mark.asyncio
@pytest.mark.parametrize("account", ACCOUNTS)
async def test_pending_missing_mode_geometry_waits_without_rewriting_candidate(account):
    candidate = frozen(account)
    before = copy.deepcopy(candidate)
    status, _ = await paper._pending_primary_buy_confirmation(
        None, account_name=account, source="tenbagger_midline", candidate=candidate,
        spot=quote(limit_down=None), limit_price=10.11, now=AT)
    assert status == "waiting" and candidate == before


@pytest.mark.asyncio
@pytest.mark.parametrize("contract", (None, "old"))
async def test_legacy_pending_cannot_borrow_new_entry_contract(contract):
    candidate = {**frozen("challenger_e"), "entry_mode_contract": contract}
    status, reason = await paper._pending_primary_buy_confirmation(
        None, account_name="challenger_e", source="tenbagger_midline",
        candidate=candidate, spot=quote(), limit_price=10.11, now=AT)
    assert status == "canceled" and "合同" in reason


@pytest.mark.asyncio
async def test_touch_order_cannot_silently_become_a_non_touch_entry(monkeypatch):
    candidate = {**frozen("challenger_e"), "entry_variant": "e2_limit_touch"}
    monkeypatch.setattr(paper, "_tenbagger_midline_candidates",
                        AsyncMock(return_value=([frozen("challenger_e")], [])))
    status, reason = await paper._pending_primary_buy_confirmation(
        None, account_name="challenger_e", source="tenbagger_midline",
        candidate=candidate, spot=quote(), limit_price=11., now=AT)
    assert status == "canceled" and "触板" in reason


@pytest.mark.parametrize("versioner", (experiment.execution_version, experiment.standard_execution_version))
def test_new_contract_only_rotates_e_and_e2_not_other_accounts(versioner, monkeypatch):
    before = {a: versioner("base", a) for a in ACCOUNT_NAMES}
    monkeypatch.setattr(experiment, "HIGHBOARD_ENTRY_MODE_CONTRACT_VERSION", "different-contract")
    after = {a: versioner("base", a) for a in ACCOUNT_NAMES}
    assert {a for a in before if before[a] != after[a]} == set(ACCOUNTS)
    assert all(len(v) <= 64 for v in after.values())


@pytest.mark.asyncio
@pytest.mark.parametrize("break_kind", ("candidate_reject", "quote_reset", "unknown_contract", "capacity"))
async def test_highboard_confirmation_does_not_bridge_invalid_mode_frames(
    memory_session, monkeypatch, break_kind,
):
    db = memory_session
    policy = dict(min_samples=2, min_persistence_sec=60, max_sample_gap_sec=90,
                  clock_jitter_sec=0., max_pullback_from_high_pct=2.5)
    monkeypatch.setattr(paper, "account_confirmation_policy", lambda _: policy)
    version = paper._strategy_version("challenger_e")
    base = dict(account_id=13, trade_date=DAY, code="001234", strategy_version=version,
                trigger="intraday", run_id="fixture", reason="fixture")
    payload = {**frozen("challenger_e"), "confirmation_version": "champion_persistent_v1",
               "confirmation_sample_at": (AT-timedelta(seconds=60)).isoformat(),
               "confirmation_source_quote_at": (AT-timedelta(seconds=60)).isoformat()}
    db.add(PaperAutoTradeLog(**base, source="tenbagger_midline", action="confirm_buy",
        decision="waiting", created_at=AT-timedelta(seconds=60), candidate_json=json.dumps(payload)))
    await db.flush()
    bad = {"highboard_confirmation_reset": True} if break_kind == "quote_reset" else {}
    if break_kind == "unknown_contract":
        bad = {**payload, "entry_mode_contract": "old",
               "confirmation_sample_at": (AT-timedelta(seconds=30)).isoformat()}
    db.add(PaperAutoTradeLog(**base,
        source="candidate" if break_kind == "candidate_reject" else "tenbagger_midline",
        action=("candidate_reject" if break_kind == "candidate_reject" else
                "confirm_buy" if break_kind == "unknown_contract" else "skip_buy"),
        decision="wait", created_at=AT-timedelta(seconds=30), candidate_json=json.dumps(bad)))
    await db.flush()
    ready, count, duration = await paper._champion_intraday_confirmation_status(
        db, account_id=13, trade_date=DAY, code="001234", source="tenbagger_midline",
        current_at=AT, account_name="challenger_e", current_entry_mode="e2_strong_entry")
    assert ready is (break_kind == "capacity")
    assert count == (2 if break_kind == "capacity" else 1)
    assert duration == (60 if break_kind == "capacity" else 0)


MODE_CASES = [
    ("tenbagger", "e_low_entry", {}),
    ("tenbagger", "e_low_recovery", dict(
        price=9.6, open=9.5, high=9.7, low=9.4, avg_price=9.55, change_pct=-4.)),
    ("challenger_e", "e2_strong_entry", {}),
    ("challenger_e", "e2_strong_recovery", dict(open=9.8, low=9.7)),
    ("challenger_e", "e2_limit_touch", dict(
        price=11., high=11., avg_price=10.7, change_pct=10.)),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("account,mode,fields", MODE_CASES)
@pytest.mark.parametrize("invalid", (None, "bad-mode", [], True))
async def test_pending_requires_original_legal_mode_before_quote_unknown(account, mode, fields, invalid):
    candidate = {**frozen(account), "entry_variant": invalid}
    before = copy.deepcopy(candidate)
    status, reason = await paper._pending_primary_buy_confirmation(
        None, account_name=account, source="tenbagger_midline",
        candidate=candidate, spot=quote(price=None), limit_price=10.11, now=AT)
    assert status == "canceled" and "模式身份" in reason
    assert candidate == before


@pytest.mark.asyncio
@pytest.mark.parametrize("account,mode,fields", MODE_CASES)
async def test_pending_mode_is_frozen_in_both_directions(monkeypatch, account, mode, fields):
    current = {**frozen(account), "entry_variant": mode}
    monkeypatch.setattr(paper, "_tenbagger_midline_candidates",
                        AsyncMock(return_value=([current], [])))
    # Includes a mode from the other account, missing contract is tested separately.
    for original in ("e_low_entry", "e_low_recovery", "e2_strong_entry",
                     "e2_strong_recovery", "e2_limit_touch"):
        candidate = {**frozen(account), "entry_variant": original}
        before = copy.deepcopy(candidate)
        status, _ = await paper._pending_primary_buy_confirmation(
            None, account_name=account, source="tenbagger_midline", candidate=candidate,
            spot=quote(**fields), limit_price=11., now=AT)
        assert status == ("valid" if original == mode else "canceled")
        assert candidate == before


@pytest.mark.asyncio
@pytest.mark.parametrize("account,mode,fields", MODE_CASES)
@pytest.mark.parametrize("previous_mode", (
    "e_low_entry", "e_low_recovery", "e2_strong_entry",
    "e2_strong_recovery", "e2_limit_touch", None, "bad-mode"))
async def test_confirmation_cannot_borrow_a_different_mode(
    memory_session, monkeypatch, account, mode, fields, previous_mode,
):
    monkeypatch.setattr(paper, "account_confirmation_policy", lambda _: dict(
        min_samples=2, min_persistence_sec=60, max_sample_gap_sec=90,
        clock_jitter_sec=0., max_pullback_from_high_pct=2.5))
    db = memory_session
    version = paper._strategy_version(account)
    base = dict(account_id=13, trade_date=DAY, code="001234", strategy_version=version,
                trigger="intraday", run_id="fixture", reason="fixture",
                source="tenbagger_midline", action="confirm_buy", decision="waiting")
    payload = {**frozen(account), "confirmation_version": "champion_persistent_v1"}
    # An older matching frame cannot be spliced past a newer different/unknown mode.
    for seconds, previous in ((90, mode), (60, previous_mode)):
        at = AT - timedelta(seconds=seconds)
        db.add(PaperAutoTradeLog(**base, created_at=at, candidate_json=json.dumps({
            **payload, "entry_variant": previous, "confirmation_sample_at": at.isoformat(),
            "confirmation_source_quote_at": at.isoformat()})))
    await db.flush()
    ready, count, duration = await paper._champion_intraday_confirmation_status(
        db, account_id=13, trade_date=DAY, code="001234", source="tenbagger_midline",
        current_at=AT, account_name=account, current_entry_mode=mode)
    same = previous_mode == mode
    assert ready is same
    assert (count, duration) == ((3, 90.) if same else (1, 0.))


@pytest.mark.asyncio
@pytest.mark.parametrize("account", ACCOUNTS)
@pytest.mark.parametrize("mode", (None, "wrong", [], True))
async def test_confirmation_unknown_current_mode_never_creates_a_sample(account, mode):
    assert await paper._champion_intraday_confirmation_status(
        None, account_id=13, trade_date=DAY, code="001234", source="tenbagger_midline",
        current_at=AT, account_name=account, current_entry_mode=mode) == (False, 0, 0.)
