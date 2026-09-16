"""Only owned frozen fixtures; never runs a broker or reads a business database."""
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import date, datetime, time, timedelta

import pytest

from app.paper.capacity_research import (
    ARMS, CapacityScope, CapacitySignal, CapacityObservation, CapacityPolicy,
    NonCapacityGuards, run_capacity_study,
)
from app.paper.signal_research import MarkoutPolicy

DAY = date(2026, 9, 8)
SHA = "a" * 64
PASS = NonCapacityGuards(True, True, True, True, True, True)


def at(hour, minute=0, second=0):
    return datetime.combine(DAY, time(hour, minute, second))


def scope(**kwargs):
    fields = dict(account_id=2, account_name="default", strategy_version="frozen-v1",
                  trade_date=DAY, observed_at=at(9, 30), window_end=at(15),
                  available_cash=4000, per_order_cash_limit=1300,
                  max_positions=2, max_daily_buys=2, used_daily_buys=0,
                  encumbered_codes=(), evidence_sha256=SHA, registered_trade_day=True)
    return CapacityScope(**(fields | kwargs))


def signal(id="a", code="600001", clock=None, **kwargs):
    clock = clock or at(9, 35)
    fields = dict(signal_id=id, account_id=2, account_name="default", strategy_version="frozen-v1",
                  code=code, confirmed_at=clock, expires_at=at(15),
                  quantity=100, limit_price=10, priority_score=1,
                  priority_at=clock, evidence_sha256=SHA)
    return CapacitySignal(**(fields | kwargs))


def obs(s, clock=None, **kwargs):
    clock = clock or s.confirmed_at
    fields = dict(signal_id=s.signal_id, account_id=s.account_id, account_name=s.account_name,
                  strategy_version=s.strategy_version, code=s.code, source_at=clock,
                  received_at=clock, observed_at=clock, guard_at=clock,
                  reference_price=9.99, quote_round_id="round-" + clock.isoformat(),
                  guards=PASS, evidence_sha256=SHA)
    return CapacityObservation(**(fields | kwargs))


def selected(report, arm):
    return [s["signal_id"] for s in report["arms"][arm]["selected"]]


def test_three_arms_share_capacity_without_claiming_executable_returns():
    a = signal(expires_at=at(9, 40))
    b = signal("b", "600002", at(9, 35, 1), expires_at=at(9, 40), priority_score=9)
    c = signal("c", "600003", at(13), priority_score=10)
    policy = CapacityPolicy(fixed_times=(time(13, 1),))
    signals, rows = [a, b, c], [obs(a), obs(b), obs(c)]
    before = deepcopy((signals, rows, asdict(scope()), asdict(policy)))
    report = run_capacity_study(scope(), signals, rows, as_of=at(15), policy=policy)
    assert selected(report, "first_come") == ["a", "b"]
    assert selected(report, "fixed_times") == ["c"]
    assert selected(report, "reserve_late") == ["a", "c"]
    for arm in report["arms"].values():
        assert arm["selected_count"] <= 2
        assert arm["reserved_cash"] + arm["unreserved_cash"] == 4000
        assert arm["executable_net_pnl"] is None
        assert all(r["actual_fill_quantity"] is None and r["fill_status"] == "unverified" for r in arm["selected"])
    assert before == (signals, rows, asdict(scope()), asdict(policy))
    assert report["winner"] is None and report["performance_difference"] is None
    assert report["contract"]["production_policy_changed"] is False
    assert report["contract"]["sell_or_same_day_t1_reuse"] is False


def test_fixed_time_priority_uses_only_confirmation_score_not_arrival_or_outcome():
    a, b = signal(), signal("b", "600002", at(9, 35, 1), priority_score=9)
    policy = CapacityPolicy(fixed_times=(time(9, 36),))
    r = run_capacity_study(scope(max_positions=1), [b, a], [obs(b), obs(a)], as_of=at(9, 36), policy=policy)
    assert selected(r, "first_come") == ["a"]
    assert selected(r, "fixed_times") == ["b"]
    assert r["arms"]["fixed_times"]["selected"][0]["selected_at"] == at(9, 36).isoformat()


def test_future_candidates_and_quotes_cannot_change_prior_inputs_or_allocations():
    a = signal()
    cutoff = at(9, 36)
    baseline = run_capacity_study(scope(), [a], [obs(a)], as_of=cutoff)
    future = signal("z", "600009", at(13), priority_score=100000)
    result = run_capacity_study(scope(), [future, a], [obs(future), obs(a)], as_of=cutoff)
    assert result == baseline


@pytest.mark.parametrize("key", ["original_trigger", "current_shape", "data_quality",
                                "ranking_contract", "execution_constraints", "risk_excluding_capacity"])
@pytest.mark.parametrize("value,prefix", [(False, "non_capacity_blocked"), (None, "non_capacity_unknown")])
def test_no_treatment_can_override_non_capacity_constraints(key, value, prefix):
    a = signal(account_name="promotion")
    o = obs(a, guards=replace(PASS, **{key: value}))
    policy = CapacityPolicy(fixed_times=(time(9, 35),), reserve_until=time(9, 35))
    result = run_capacity_study(scope(account_name="promotion"), [a], [o], as_of=at(9, 35), policy=policy)
    for arm in ARMS:
        assert selected(result, arm) == []
        assert result["arms"][arm]["terminal_states"]["a"] == prefix + ":" + key


def test_expired_original_trigger_is_not_extended_to_treatment_time():
    a = signal(expires_at=at(9, 35, 30))
    result = run_capacity_study(scope(), [a], [obs(a)], as_of=at(10))
    assert selected(result, "fixed_times") == []
    assert result["arms"]["fixed_times"]["terminal_states"]["a"] == "original_signal_expired"
    assert selected(result, "first_come") == ["a"]


def test_fixed_and_reserved_decisions_need_fresh_quotes_not_morning_signal_price():
    a = signal()
    p = CapacityPolicy(fixed_times=(time(13),), reserve_slots=2)
    r = run_capacity_study(scope(), [a], [obs(a)], as_of=at(13), policy=p)
    assert selected(r, "first_come") == ["a"]
    for arm in ("fixed_times", "reserve_late"):
        assert selected(r, arm) == []
        assert r["arms"][arm]["terminal_states"]["a"] == "quote_stale_at_treatment_time"
    fresh = obs(a, at(13))
    ready = run_capacity_study(scope(), [a], [obs(a), fresh], as_of=at(13), policy=p)
    assert selected(ready, "fixed_times") == ["a"]
    assert selected(ready, "reserve_late") == ["a"]


def test_fresh_shape_invalidation_is_not_overridden_by_old_confirmed_signal():
    a = signal()
    current = obs(a, at(9, 36), guards=replace(PASS, current_shape=False))
    r = run_capacity_study(scope(), [a], [obs(a), current], as_of=at(9, 36),
                           policy=CapacityPolicy(fixed_times=(time(9, 36),)))
    assert selected(r, "first_come") == ["a"]
    assert selected(r, "fixed_times") == []
    assert r["arms"]["fixed_times"]["terminal_states"]["a"] == "non_capacity_blocked:current_shape"


def test_cash_and_per_request_caps_include_full_lot_and_commission():
    a, b = signal(), signal("b", "600002", at(9, 35, 1))
    r = run_capacity_study(scope(available_cash=2009.99), [a, b], [obs(a), obs(b)], as_of=at(10))
    assert selected(r, "first_come") == ["a"]
    assert r["arms"]["first_come"]["reserved_cash"] == 1005
    assert r["arms"]["first_come"]["unreserved_cash"] == 1004.99
    assert r["arms"]["first_come"]["terminal_states"]["b"] == "original_available_cash_insufficient"
    small = run_capacity_study(scope(per_order_cash_limit=1004.99), [a], [obs(a)], as_of=at(10))
    assert selected(small, "first_come") == []
    assert small["arms"]["first_come"]["terminal_states"]["a"] == "original_per_order_budget_exceeded"


def test_reserved_cash_floor_and_slots_are_separate_constraints():
    a, b = signal(), signal("b", "600002", at(9, 35, 1))
    p = CapacityPolicy(reserve_slots=0, reserve_cash_fraction=0.75)
    r = run_capacity_study(scope(), [a, b], [obs(a), obs(b)], as_of=at(10), policy=p)
    assert selected(r, "reserve_late") == []
    assert all(v == "reserved_for_late_session" for v in r["arms"]["reserve_late"]["terminal_states"].values())
    slots = run_capacity_study(scope(), [a, b], [obs(a), obs(b)], as_of=at(10),
                               policy=CapacityPolicy(reserve_cash_fraction=0, reserve_slots=1))
    assert selected(slots, "reserve_late") == ["a"]


def test_original_held_pending_codes_and_used_buy_slots_are_never_recycled():
    a, b = signal(), signal("b", "600002", at(9, 35, 1))
    r = run_capacity_study(scope(encumbered_codes=("600001",), used_daily_buys=1),
                           [a, b], [obs(a), obs(b)], as_of=at(10))
    assert selected(r, "first_come") == ["b"]
    assert r["arms"]["first_come"]["terminal_states"]["a"] == "held_or_pending_code_no_add_on"
    assert r["arms"]["first_come"]["remaining_new_slots"] == 0
    no_slots = run_capacity_study(scope(used_daily_buys=2), [b], [obs(b)], as_of=at(10))
    assert selected(no_slots, "first_come") == []


def test_repeated_same_stock_signal_does_not_manufacture_an_independent_sample():
    a = signal(expires_at=at(9, 35, 10))
    b = signal("b", "600001", at(9, 36), priority_score=100)
    r = run_capacity_study(scope(), [a, b], [obs(a), obs(b)], as_of=at(10))
    assert r["signal_count"] == 2 and r["first_account_version_day_code_count"] == 1
    assert r["arms"]["first_come"]["terminal_states"]["b"] == "repeated_same_day_code_not_independent_sample"
    assert selected(r, "first_come") == ["a"]


def test_commission_is_a_conservative_cash_reservation_not_claimed_real_fee():
    a = signal(limit_price=333.34)
    p = CapacityPolicy(costs=MarkoutPolicy(slippage_pct=0))
    r = run_capacity_study(scope(available_cash=100000, per_order_cash_limit=50000),
                           [a], [obs(a, reference_price=333.34)], as_of=at(10), policy=p)
    assert r["arms"]["first_come"]["reserved_cash"] == 33344.01
    assert r["arms"]["first_come"]["selected"][0]["quantity"] == 100
    assert "commission_ceiling_reservation" in r["policy"]["costs_scope"]


def test_limit_price_and_slippage_guard_never_resizes_or_chases_the_request():
    a = signal()
    r = run_capacity_study(scope(), [a], [obs(a, reference_price=10)], as_of=at(10))
    assert selected(r, "first_come") == []
    assert r["arms"]["first_come"]["terminal_states"]["a"] == "original_limit_price_exceeded"


def test_hashes_bind_scope_cost_policy_expired_and_blocked_evidence():
    a = signal()
    o = obs(a, guards=replace(PASS, data_quality=False))
    r = run_capacity_study(scope(), [a], [o], as_of=at(10))
    changed = run_capacity_study(scope(evidence_sha256="b"*64), [a], [o], as_of=at(10))
    assert r["input_sha256"] != changed["input_sha256"]
    assert r["report_sha256"] != changed["report_sha256"]
    policy = run_capacity_study(scope(), [a], [o], as_of=at(10),
                                policy=CapacityPolicy(reserve_cash_fraction=0.5))
    assert r["input_sha256"] == policy["input_sha256"]
    assert r["policy"]["version"] != policy["policy"]["version"]
    assert r["report_sha256"] != policy["report_sha256"]


def test_missing_observations_are_unknown_not_free_slots_or_zero_profit():
    a = signal()
    r = run_capacity_study(scope(), [a], [], as_of=at(9, 36))
    assert r["arms"]["first_come"]["terminal_states"]["a"] == "awaiting_observation"
    assert r["arms"]["fixed_times"]["terminal_states"]["a"] == "fixed_treatment_not_elapsed"
    assert all(arm["executable_net_pnl"] is None for arm in r["arms"].values())


def test_empty_data_keeps_all_arms_and_same_budget_without_winner():
    r = run_capacity_study(scope(), [], [], as_of=at(15))
    assert set(r["arms"]) == set(ARMS)
    assert all(arm["selected_count"] == 0 and arm["unreserved_cash"] == 4000 for arm in r["arms"].values())
    assert r["winner"] is None


@pytest.mark.parametrize("change", [
    {"available_cash": -1}, {"available_cash": float("nan")}, {"available_cash": True},
    {"max_positions": 1.5}, {"account_id": True}, {"account_name": "unknown"},
    {"registered_trade_day": False}, {"registered_trade_day": 1},
    {"encumbered_codes": ["600001"]}, {"encumbered_codes": ("600001", "600001")},
    {"observed_at": at(12)}, {"window_end": at(9, 30)}, {"evidence_sha256": "missing"},
])
def test_invalid_initial_scope_fails_closed(change):
    with pytest.raises(ValueError):
        scope(**change)


@pytest.mark.parametrize("change", [
    {"quantity": 0}, {"quantity": 50}, {"quantity": True},
    {"limit_price": float("inf")}, {"limit_price": "10"}, {"limit_price": 10.001},
    {"priority_score": float("nan")}, {"priority_score": True},
    {"priority_at": at(9, 36)}, {"expires_at": at(9, 34)},
])
def test_invalid_original_request_is_not_repaired(change):
    with pytest.raises(ValueError):
        signal(**change)


def test_clock_conflicts_identity_and_duplicate_inputs_are_rejected():
    a = signal()
    o = obs(a)
    for signals, rows in [([a, a], [o]), ([a], [o, o]), ([a], [replace(o, code="600009")]),
                          ([a], [replace(o, signal_id="unknown")])]:
        with pytest.raises(ValueError):
            run_capacity_study(scope(), signals, rows, as_of=at(10))
    with pytest.raises(ValueError, match="source/receipt"):
        obs(a, source_at=at(9, 36))
    with pytest.raises(ValueError, match="conflicting price"):
        late = obs(a, at(9, 36), source_at=a.confirmed_at, reference_price=9.98)
        run_capacity_study(scope(), [a], [o, late], as_of=at(10))
    with pytest.raises(ValueError, match="conflicting guards"):
        late = obs(a, at(9, 36), source_at=a.confirmed_at, received_at=a.confirmed_at,
                   guard_at=a.confirmed_at, guards=replace(PASS, original_trigger=False))
        run_capacity_study(scope(), [a], [o, late], as_of=at(10))


def test_received_old_quote_cannot_reinstate_newer_invalidated_state():
    a = signal()
    first = obs(a, at(9, 36))
    late_old = obs(a, at(9, 37), source_at=at(9, 35))
    with pytest.raises(ValueError, match="out-of-order"):
        run_capacity_study(scope(), [a], [first, late_old], as_of=at(10))


def test_quote_can_precede_confirmation_receipt_but_guard_must_not():
    a = signal()
    legitimate = obs(a, source_at=at(9, 34, 59), received_at=at(9, 34, 59))
    r = run_capacity_study(scope(), [a], [legitimate], as_of=at(9, 35))
    assert selected(r, "first_come") == ["a"]
    with pytest.raises(ValueError, match="pre-confirmation"):
        old_guard = obs(a, source_at=at(9, 34, 59), received_at=at(9, 34, 59), guard_at=at(9, 34, 59))
        run_capacity_study(scope(), [a], [old_guard], as_of=at(10))


def test_lunch_is_not_an_observation_or_a_bridge_for_stale_quote():
    a = signal()
    with pytest.raises(ValueError, match="outside regular"):
        obs(a, at(12))
    with pytest.raises(ValueError, match="fixed market"):
        CapacityPolicy(fixed_times=(time(12),))


def test_guard_truthiness_and_policy_numbers_do_not_silently_pass():
    with pytest.raises(ValueError, match="true, false"):
        replace(PASS, original_trigger=1)
    for values in ({"reserve_cash_fraction": float("nan")}, {"reserve_slots": True},
                   {"max_guard_age_sec": 0}, {"fixed_times": (time(13), time(10))}):
        with pytest.raises(ValueError):
            CapacityPolicy(**values)


def test_asof_before_scope_and_prior_day_signal_are_rejected():
    with pytest.raises(ValueError, match="precedes"):
        run_capacity_study(scope(), [], [], as_of=at(9))
    a = signal()
    old = replace(a, confirmed_at=a.confirmed_at-timedelta(days=1),
                  expires_at=a.expires_at-timedelta(days=1), priority_at=a.priority_at-timedelta(days=1))
    with pytest.raises(ValueError, match="outside frozen"):
        run_capacity_study(scope(), [old], [], as_of=at(10))


def test_stale_guard_can_block_fresh_source_even_with_generous_quote_ttl():
    a = signal()
    p = CapacityPolicy(fixed_times=(time(9, 36),), max_guard_age_sec=10, max_quote_age_sec=120)
    r = run_capacity_study(scope(), [a], [obs(a)], as_of=at(9, 36), policy=p)
    assert r["arms"]["fixed_times"]["terminal_states"]["a"] == "guards_stale_at_treatment_time"


def frozen_payload():
    import json
    a = signal()
    return json.loads(json.dumps({
        "schema": "paper_capacity_frozen_input_v1", "scope": asdict(scope()),
        "signals": [asdict(a)], "observations": [asdict(obs(a))], "as_of": at(10),
    }, default=lambda x: x.isoformat()))


def test_cli_only_reads_supplied_json_publishes_atomic_0600_and_never_overwrites(tmp_path, monkeypatch):
    import json
    import hashlib
    import stat
    from scripts import paper_capacity_research as cli
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    source = tmp_path / "frozen.json"
    source.write_text(json.dumps(frozen_payload()), encoding="utf-8")
    before = source.read_bytes()
    destination = tmp_path / "outputs" / "study.json"
    assert cli.main(["--input", str(source), "--output", str(destination)]) == 0
    content = destination.read_bytes()
    output = json.loads(content)
    assert source.read_bytes() == before
    assert output["input_sha256"] == hashlib.sha256(before).hexdigest()
    assert output["database_connected"] is False and output["orders_submitted"] is False
    assert output["study"]["contract"]["pending_cancel_and_queue_fill_simulated"] is False
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    assert list(destination.parent.iterdir()) == [destination]
    with pytest.raises(ValueError, match="new and under"):
        cli.main(["--input", str(source), "--output", str(destination)])
    assert destination.read_bytes() == content


def test_cli_refuses_unknown_outcome_fields_nan_duplicate_keys_and_path_escape(tmp_path, monkeypatch):
    import json
    from scripts import paper_capacity_research as cli
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    source = tmp_path / "input.json"
    destination = tmp_path / "outputs" / "out.json"
    payload = frozen_payload()
    for raw, message in [
        (json.dumps(payload | {"future_profit": 3.0}), "unknown top-level"),
        (json.dumps(payload).replace('"available_cash": 4000', '"available_cash": NaN'), "non-finite JSON"),
        (json.dumps(payload).replace('"original_trigger": true', '"original_trigger": false, "original_trigger": true'), "duplicate JSON"),
    ]:
        source.write_text(raw)
        with pytest.raises(ValueError, match=message):
            cli.main(["--input", str(source), "--output", str(destination)])
        assert not destination.exists()
    source.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="under project outputs"):
        cli.main(["--input", str(source), "--output", str(tmp_path / "escape.json")])


def test_cli_rejects_future_asof_and_does_not_accept_outcome_fields_in_candidates():
    from scripts import paper_capacity_research as cli
    payload = frozen_payload()
    payload["as_of"] = "2999-01-01T00:00:00"
    with pytest.raises(ValueError, match="non-future"):
        cli.decode_input(payload)
    payload = frozen_payload()
    payload["signals"][0]["future_return"] = 100
    with pytest.raises(TypeError, match="unexpected keyword"):
        cli.decode_input(payload)


@pytest.mark.parametrize("field,value", [("account_id", 3), ("account_name", "promotion"),
                                         ("strategy_version", "old-version")])
def test_no_merging_across_same_name_instances_accounts_or_frozen_versions(field, value):
    a = signal()
    mixed = replace(a, **{field: value})
    with pytest.raises(ValueError, match="signal account instance/version"):
        run_capacity_study(scope(), [mixed], [obs(mixed)], as_of=at(10))
    with pytest.raises(ValueError, match="observation account instance/version"):
        run_capacity_study(scope(), [a], [replace(obs(a), **{field: value})], as_of=at(10))


def test_simultaneous_fresh_recheck_uses_original_time_and_does_not_scan_unrelated_stale_names():
    a, b = signal(), signal("b", "600002", at(9, 36))
    blocked = obs(a, guards=replace(PASS, original_trigger=False))
    a_fresh = obs(a, at(9, 36))
    r = run_capacity_study(scope(max_positions=1), [a, b], [obs(b), a_fresh, blocked], as_of=at(10))
    assert selected(r, "first_come") == ["a"]
    assert r["arms"]["first_come"]["terminal_states"]["b"] == "original_position_or_daily_capacity_full"
    assert r["contract"]["counterfactual_nav_risk_recomputed"] is False
