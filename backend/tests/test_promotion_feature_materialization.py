from datetime import date, datetime, timedelta

import pytest

from app.core.trade_calendar import is_official_closed_day
from app.promotion.modeling.daily_materials import MaterialArchive, ReviewedSourcePolicy, encode, decode
from app.promotion.modeling.daily_materialization import materialize, lock_ready, bind_candidate, _window
from app.promotion.modeling.feature_coverage import hist_feature_coverage
from app.promotion.modeling.features import FEATURE_VERSION
from app.promotion.modeling.historical_dataset import _Bar, _historical_values

DAY = date(2026, 9, 7)


def fixture_parser(raw):
    # ONLY an isolated reviewed-test protocol. No shipped vendor is authorized.
    message = decode(raw)
    if message.pop("protocol_fixture", None) != "complete_final_response_test_only":
        raise ValueError("fixture protocol signature missing")
    return message


def inputs(tmp_path, mutation=None):
    policy = ReviewedSourcePolicy("isolated_fixture_policy_v1", {"ths:fixture_only": fixture_parser})
    archive = MaterialArchive(tmp_path, policy=policy)
    calendar = {}
    cursor = DAY - timedelta(days=150)
    while cursor <= DAY:
        calendar[cursor] = cursor.weekday() < 5 and not is_official_closed_day(cursor)
        cursor += timedelta(days=1)
    days = _window(DAY, calendar)
    codes = ["600001", "600002"]
    def make(kind, day):
        rows = []
        for code in codes:
            if kind == "close":
                row = dict(code=code, open=10, high=11, low=9, close=10, prev_close=10,
                           volume=10000, amount=100000, turnover=1, change_pct=0, status="ok")
            elif kind == "fund":
                row = dict(code=code, main_net_inflow=0, main_net_inflow_pct=0, status="ok")
            else:
                row = dict(code=code, status="ok")
            rows.append(row)
        p = dict(protocol_fixture="complete_final_response_test_only", kind=kind,
            source="ths", source_version="fixture_only",
            trade_date=day.isoformat(), source_quote_at=f"{day}T16:00:00",
            universe_frozen_at=f"{day}T08:00:00", complete=True, finality_verified=True,
            universe_verified=True, expected_from_protocol=True, universe_codes=codes,
            expected_codes=codes, rows=rows, price_basis="CNY_per_share",
            adjustment_basis="forward_adjusted", adjustment_version="fixture_adj1",
            volume_unit="shares", amount_unit="CNY")
        if mutation:
            mutation(p)
        return archive.archive(encode(p), {"source": "ths", "source_version": "fixture_only",
                                           "provenance": "forward_response"})
    close_refs = [make("close", d) for d in days]
    fund_refs = [make("fund", d) for d in days[-3:]]
    pool_ref = make("pool", DAY)
    return archive, dict(trade_date=DAY, close_refs=close_refs, fund_refs=fund_refs,
                         pool_ref=pool_ref, calendar=calendar), days


def cutoff():
    return datetime.now()


def test_ready_exact_old_formula_true_zero_and_binding(tmp_path):
    archive, args, days = inputs(tmp_path)
    result = materialize(archive, **args)
    assert result["status"] == "ready", result
    frozen_at = cutoff()
    locked = lock_ready(archive, trade_date=DAY, cutoff=frozen_at)
    bound = bind_candidate(locked, code="600001")
    bars = [_Bar("600001", d, 10, 11, 9, 10, 10000, 100000, 1, 0, 10) for d in days]
    expected, _ = _historical_values(bars, 60,
        fund_map={("600001", d): (0, 0) for d in days[-3:]},
        market_context={DAY: dict(advance_ratio=0, limit_up_count=0, median_return=0, return_dispersion=0)})
    assert bound["values"] == expected
    assert bound["values"]["hist_fund_3d_sum"] == 0
    assert bound["status"] == "retrospective_export_only"
    assert "hist_materialization" not in bound
    # A pure binder fixture independently verifies the same-day envelope. This
    # does not claim the historical source archive was available on a newer day.
    from dataclasses import replace
    p = decode(locked.payload)
    p["trade_date"] = frozen_at.date().isoformat()
    same_day_fixture = replace(locked, payload=encode(p))
    same_day = bind_candidate(same_day_fixture, code="600001")
    assert same_day["status"] == "ready"
    assert hist_feature_coverage(same_day["values"], same_day["values"],
        materialization=same_day["hist_materialization"], code="600001",
        trade_date=frozen_at.date().isoformat(), as_of_at=frozen_at, feature_version=FEATURE_VERSION)["passed"]
    assert bind_candidate(locked, code="600003")["status"] == "blocked"


@pytest.mark.parametrize("bad", ["missing_fund", "source_clock", "universe_clock", "finality", "pool_completion",
    "expected", "duplicate_code", "missing_code", "unknown_row", "basis", "unit", "nan", "ohlc"])
def test_material_gaps_never_turn_into_ready(tmp_path, bad):
    def change(p):
        if p["kind"] == "fund" and bad == "missing_fund":
            p["rows"][0].pop("main_net_inflow")
        if p["kind"] != "pool":
            return
        if bad == "source_clock":
            p["source_quote_at"] = None
        elif bad == "universe_clock":
            p["universe_frozen_at"] = "2999-01-01T00:00:00"
        elif bad == "finality":
            p["finality_verified"] = False
        elif bad == "pool_completion":
            p["complete"] = False
        elif bad == "expected":
            p["expected_from_protocol"] = False
        elif bad == "duplicate_code":
            p["rows"].append(p["rows"][0])
        elif bad == "missing_code":
            p["rows"].pop()
        elif bad == "unknown_row":
            p["rows"][0]["status"] = "quarantined"
        elif bad == "basis":
            p["adjustment_version"] = "other"
        elif bad == "unit":
            p["volume_unit"] = "lots"
    def combined(p):
        change(p)
        if p["kind"] == "close" and bad == "nan":
            p["rows"][0]["volume"] = None
        if p["kind"] == "close" and bad == "ohlc":
            p["rows"][0]["low"] = 20
    archive, args, _ = inputs(tmp_path, combined)
    result = materialize(archive, **args)
    assert result["status"] == "blocked"
    assert not list((tmp_path / "ready").glob("*.blob"))


@pytest.mark.parametrize("bad", ["missing_session", "duplicate_session", "calendar_gap", "fund_window"])
def test_windows_and_calendar_are_exact(tmp_path, bad):
    archive, args, days = inputs(tmp_path)
    if bad == "missing_session":
        args["close_refs"].pop(0)
    elif bad == "duplicate_session":
        args["close_refs"][-1] = args["close_refs"][0]
    elif bad == "fund_window":
        args["fund_refs"].pop(0)
    else:
        args["calendar"].pop(days[-2])
    assert materialize(archive, **args)["status"] == "blocked"


def test_cutoff_microseconds_and_locked_revision_do_not_float(tmp_path):
    archive, args, _ = inputs(tmp_path)
    first = materialize(archive, **args)
    first_receipt = decode(archive.read_bytes(first["receipt_ref"]))
    published = datetime.fromisoformat(first_receipt["published_at"])
    assert lock_ready(archive, trade_date=DAY, cutoff=published.replace(microsecond=0)) is None
    lock = lock_ready(archive, trade_date=DAY, cutoff=cutoff())
    before = bind_candidate(lock, code="600001")
    second = materialize(archive, **args)
    assert first["receipt_ref"] != second["receipt_ref"]
    assert bind_candidate(lock, code="600001") == before
    later = lock_ready(archive, trade_date=DAY, cutoff=cutoff())
    assert later.receipt != lock.receipt


def test_physical_source_lost_or_modified_rejects_lock(tmp_path):
    archive, args, _ = inputs(tmp_path)
    assert materialize(archive, **args)["status"] == "ready"
    manifest = decode(archive.read_bytes(args["close_refs"][0]))
    raw_ref = manifest["raw_ref"]
    path = archive.root / raw_ref["path"]
    raw = path.read_bytes()
    path.write_bytes(b"x" + raw[1:])
    with pytest.raises(ValueError, match="SHA"):
        lock_ready(archive, trade_date=DAY, cutoff=cutoff())


def test_no_reviewed_protocol_even_with_formal_source_stays_blocked(tmp_path):
    archive, args, _ = inputs(tmp_path)
    unreviewed = MaterialArchive(tmp_path)
    assert materialize(unreviewed, **args)["status"] == "blocked"
    assert bind_candidate(None, code="600001")["status"] == "blocked"


@pytest.mark.parametrize("bad", ["source_identity", "source_date_only", "source_future", "same_second_future", "materialized_backdated"])
def test_identity_and_actual_publication_clocks(tmp_path, bad):
    def mutation(p):
        if bad == "source_identity":
            p["source"] = "other"
        elif bad == "source_date_only":
            p["source_quote_at"] = p["trade_date"]
        elif bad == "source_future":
            p["source_quote_at"] = "2999-01-01T16:00:00"
    archive, args, _ = inputs(tmp_path, mutation)
    result = materialize(archive, **args)
    if bad.startswith("source"):
        assert result["status"] == "blocked"
        return
    assert result["status"] == "ready"
    receipt = decode(archive.read_bytes(result["receipt_ref"]))
    if bad == "same_second_future":
        at = datetime.fromisoformat(receipt["published_at"])
        assert lock_ready(archive, trade_date=DAY, cutoff=at - timedelta(microseconds=1)) is None
    else:
        payload = decode(archive.read_bytes(receipt["ready_ref"]))
        payload["materialized_at"] = "2020-01-01T00:00:00"
        archive.publish(payload)
        with pytest.raises(ValueError, match="input_published_after"):
            lock_ready(archive, trade_date=DAY, cutoff=cutoff())


def test_restart_values_tamper_and_observed_now_cannot_ready(tmp_path):
    archive, args, _ = inputs(tmp_path)
    result = materialize(archive, **args)
    frozen = cutoff()
    locked = lock_ready(archive, trade_date=DAY, cutoff=frozen)
    restarted = MaterialArchive(tmp_path, policy=archive.policy)
    assert lock_ready(restarted, trade_date=DAY, cutoff=frozen) == locked
    receipt = decode(archive.read_bytes(result["receipt_ref"]))
    payload = decode(archive.read_bytes(receipt["ready_ref"]))
    payload["values_by_code"]["600001"]["hist_return_1d"] = 999
    archive.publish(payload)
    with pytest.raises(ValueError, match="values_do_not_match"):
        lock_ready(archive, trade_date=DAY, cutoff=cutoff())
    # An observed-now manifest must not pass even under the reviewed test parser.
    first = archive.read(args["close_refs"][0])
    raw = archive.read_bytes(first["manifest"]["raw_ref"])
    args["close_refs"][0] = archive.archive(raw, {"source": "ths", "source_version": "fixture_only"})
    assert materialize(archive, **args)["status"] == "blocked"


def test_nonflat_formula_equality_with_real_zero_and_negative_flows(tmp_path):
    import numpy as np
    def row_values(d, code):
        value = 10 + (d.toordinal() % 90) / 100
        previous = d - timedelta(days=1)
        while previous.weekday() >= 5 or is_official_closed_day(previous):
            previous -= timedelta(days=1)
        prev = 10 + (previous.toordinal() % 90) / 100
        return dict(open=value, high=value + 0.5, low=value - 0.2, close=value,
                    prev_close=prev, volume=10000 + d.day * 10,
                    amount=200000 + d.day * 100, turnover=2, change_pct=(value / prev - 1) * 100)
    def change(p):
        d = date.fromisoformat(p["trade_date"])
        if p["kind"] == "close":
            for row in p["rows"]:
                row.update(row_values(d, row["code"]))
        if p["kind"] == "fund":
            for row in p["rows"]:
                row["main_net_inflow"] = -10 if d != DAY else 0
                row["main_net_inflow_pct"] = -0.5 if d != DAY else 0
    archive, args, days = inputs(tmp_path, change)
    result = materialize(archive, **args)
    assert result["status"] == "ready", result
    b = bind_candidate(lock_ready(archive, trade_date=DAY, cutoff=cutoff()), code="600001")
    bars = [_Bar(code="600001", trade_date=d, **row_values(d, "600001")) for d in days]
    last_change = bars[-1].change_pct
    expected, _ = _historical_values(bars, 60,
        fund_map={("600001", d): (-10, -0.5) if d != DAY else (0, 0) for d in days[-3:]},
        market_context={DAY: dict(advance_ratio=float(last_change > 0),
            limit_up_count=float(2 * (last_change >= 9.5)), median_return=last_change, return_dispersion=0)})
    assert b["values"] == expected
    assert b["values"]["hist_fund_main_net_inflow"] == 0
    assert b["values"]["hist_fund_3d_sum"] == -20


@pytest.mark.parametrize("bad", ["row_none", "row_int", "row_list", "row_string", "rows_object",
                                 "unicode_code", "kind_object", "adjustment_object", "day_object"])
def test_malformed_parser_fields_are_blocked_not_attribute_errors(tmp_path, bad):
    def change(p):
        if p["kind"] != "pool":
            return
        if bad.startswith("row_"):
            value = {"row_none": None, "row_int": 1, "row_list": [], "row_string": "x"}[bad]
            p["rows"][0] = value
        elif bad == "rows_object":
            p["rows"] = {}
        elif bad == "unicode_code":
            p["rows"][0]["code"] = "６００００１"
        elif bad == "kind_object":
            p["kind"] = {}
        elif bad == "adjustment_object":
            p["adjustment_version"] = {"fake": "v1"}
        else:
            p["trade_date"] = {}
    archive, args, _ = inputs(tmp_path, change)
    assert materialize(archive, **args)["status"] == "blocked"


def test_materialized_must_precede_receipt_even_when_both_before_cutoff(tmp_path):
    import os
    archive, args, _ = inputs(tmp_path)
    first = materialize(archive, **args)
    original = decode(archive.read_bytes(first["receipt_ref"]))
    payload = decode(archive.read_bytes(original["ready_ref"]))
    materialized = datetime.now()
    payload["materialized_at"] = materialized.isoformat()
    forged_ref = archive.put(encode(payload))
    published = materialized - timedelta(microseconds=1)
    # Fixture-only mtime corruption isolates the declared causal-clock check
    # from the independent filesystem-availability check.
    os.utime(archive.root / forged_ref["path"], (published.timestamp() - 1, published.timestamp() - 1))
    archive.put(encode({**original, "ready_ref": forged_ref, "published_at": published.isoformat()}), area="ready")
    with pytest.raises(ValueError, match="materialization_after_publication"):
        lock_ready(archive, trade_date=DAY, cutoff=cutoff())


def test_future_cutoff_rejected_without_visibility_claim(tmp_path):
    archive, args, _ = inputs(tmp_path)
    materialize(archive, **args)
    with pytest.raises(ValueError, match="cutoff_after_actual_now"):
        lock_ready(archive, trade_date=DAY, cutoff=datetime.now() + timedelta(seconds=1))


def test_equal_publication_clock_conflicting_revisions_never_sha_pick(tmp_path):
    archive, args, _ = inputs(tmp_path)
    first = materialize(archive, **args)
    receipt = decode(archive.read_bytes(first["receipt_ref"]))
    payload = decode(archive.read_bytes(receipt["ready_ref"]))
    payload["values_by_code"]["600001"]["hist_return_1d"] = 777
    other_ref = archive.put(encode(payload))
    same_time = datetime.now().isoformat()
    archive.put(encode({**receipt, "published_at": same_time}), area="ready")
    archive.put(encode({**receipt, "ready_ref": other_ref, "published_at": same_time}), area="ready")
    with pytest.raises(ValueError, match="ambiguous_same_clock"):
        lock_ready(archive, trade_date=DAY, cutoff=cutoff())


def test_owned_refs_and_calendar_survive_caller_mutation_mid_materialize(tmp_path, monkeypatch):
    archive, args, _ = inputs(tmp_path)
    original = archive.read
    changed = False
    def mutate(ref):
        nonlocal changed
        if not changed:
            changed = True
            args["calendar"].clear()
            args["close_refs"][0]["size"] = 1
            args["close_refs"].clear()
            args["fund_refs"].clear()
            args["pool_ref"].clear()
        return original(ref)
    monkeypatch.setattr(archive, "read", mutate)
    result = materialize(archive, **args)
    assert result["status"] == "ready", result
    locked = lock_ready(archive, trade_date=DAY, cutoff=cutoff())
    p = decode(locked.payload)
    assert len(p["input_refs"]["close"]) == 61 and len(p["input_refs"]["fund"]) == 3
    assert p["calendar"] and p["input_refs"]["pool"]["size"] > 1


def test_change_pct_zero_matches_existing_historical_loader_fallback(tmp_path):
    def change(p):
        if p["kind"] == "close" and p["trade_date"] == str(DAY):
            for row in p["rows"]:
                row.update(close=10.1, change_pct=0)
    archive, args, _ = inputs(tmp_path, change)
    assert materialize(archive, **args)["status"] == "ready"
    b = bind_candidate(lock_ready(archive, trade_date=DAY, cutoff=cutoff()), code="600001")
    # historical_dataset loader lines 385-389: abs(change)<1e-9 and positive
    # close/prev_close recovers percent return before _historical_values.
    assert b["values"]["hist_return_1d"] == (10.1 / 10 - 1) * 100
