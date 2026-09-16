"""Synthetic owned evidence only; default conftest owns the isolated test DB."""
from copy import deepcopy
from datetime import datetime, timedelta
import json

import pytest

from app.paper.post_trim_research import PostTrimPolicy, build_post_trim_report
from app.paper.profit_protection_research import _hash


def clock(value):
    return "2026-09-09T" + value


def fixture_case(before=200, sold=100):
    identity = dict(cycle_id="cycle-1", account_instance="account-9-incarnation-1",
                    code="000564", buy_time="2026-09-08T09:36:30", entry_version="entry-v1")
    anchor = dict(identity=deepcopy(identity), revision_basis="supplied_immutable_ledger_transition",
                  before_revision="ledger-1", after_revision="ledger-2",
                  transition_ref="transition-1", order_id="order-1", fill_summary_ref="fills-1",
                  entry_policy_revision="policy-1", entry_policy_ref="entry-order-1",
                  entry_rules_content_hash=_hash(dict(max_hold_days=5, stop_loss_pct=5,
                                                       take_profit_pct=8, forced_exit_rule_ids=[])),
                  first_trim_in_cycle=True, fill_summary_complete=True,
                  status="confirmed_partial_position", before_quantity=before,
                  filled_quantity=sold, remaining_quantity=before-sold,
                  before_revision_known_at=identity["buy_time"],
                  basis_known_at=identity["buy_time"],
                  occurred_at=clock("09:45:55"), response_observed_at=clock("09:45:57"),
                  commit_known_at=clock("09:45:58"), source="tencent",
                  price_basis="unadjusted-CNY-v1", basis_ref="basis-1",
                  corporate_action_status="none",
                  observed_state_content_hash="not-a-ledger-revision")
    anchor["fills"] = [dict(identity=deepcopy(identity), order_id="order-1", side="sell",
                            fill_id="fill-1", evidence_ref="fill-proof-1", quantity=sold,
                            price=10, occurred_at=clock("09:45:55"))]
    return dict(identity=identity, anchor=anchor, frames=[])


def frame(case, source="09:46:00", price=10, observed=None, round_id=None):
    at = clock(source)
    obs = observed or (datetime.fromisoformat(at) + timedelta(seconds=1)).isoformat()
    rid = round_id or "round-" + source
    ident = deepcopy(case["identity"])
    rules = dict(max_hold_days=5, stop_loss_pct=5, take_profit_pct=8, forced_exit_rule_ids=[])
    gate = dict(identity=deepcopy(ident), revision="ledger-2", round_id=rid,
                basis="supplied_complete_frozen_original_evaluation",
                evidence_ref="gate-" + rid, policy_ref="entry-order-1", policy_revision="policy-1",
                entry_version=ident["entry_version"], policy_frozen_at=ident["buy_time"],
                known_at=obs, triggered=True, exit_class="nonhard_protection",
                quote_content_hash=_hash(dict(source="tencent", source_quote_at=at, price=price, round_id=rid)),
                execution_price=price, evaluated_quantity=case["anchor"]["remaining_quantity"],
                frozen_rules=rules, rules_content_hash=_hash(rules), missing_keys=[],
                checks={k: dict(passed=True, evidence_ref="check-" + k)
                        for k in ("t1", "quantity", "quote", "orderbook", "other_original_blocks")})
    return dict(identity=ident, revision="ledger-2", remaining_quantity=case["anchor"]["remaining_quantity"],
                position_active=True, original_gate=gate, observed_at=obs,
                source_quote_at=at, received_at=obs, revision_known_at=clock("09:45:58"),
                evidence_ref="frame-" + rid, round_id=rid, quantity_ref="quantity-" + rid,
                price=price, price_basis="unadjusted-CNY-v1", basis_ref="basis-1",
                corporate_action_status="none", source="tencent", basis_known_at=ident["buy_time"],
                sellable_quantity=case["anchor"]["remaining_quantity"],
                observed_state_content_hash="content-only")


def calendar():
    return [dict(trade_date=d, is_trade_day=True, registered_at="2026-09-07T15:00:00",
                 evidence_ref="calendar-" + d) for d in ("2026-09-08", "2026-09-09")]


def report(case, policy=None, cal=None):
    return build_post_trim_report([case], calendar() if cal is None else cal,
                                 as_of=clock("15:00:00"),
                                 policy=policy or PostTrimPolicy("research:test", 1, 90, 90))


def rows(case, **kwargs):
    return report(case, **kwargs)["results"][0]["frames"]


def normal():
    c = fixture_case()
    c["frames"] = [frame(c), frame(c, "09:46:30", 9.8)]
    return c


def test_normal_segment_denominators_and_input_immutability():
    c = normal()
    before = deepcopy(c)
    r = report(c)
    assert c == before
    assert r["schema_version"] == "research:post_trim_v1"
    assert r["case_denominator"] == 1 and r["frame_denominator"] == 2
    assert r["results"][0]["frames"][-1]["candidate"] == "research_condition_met"
    assert r["results"][0]["candidate_minus_baseline_seconds"] == 30
    assert r["results"][0]["frames"][0]["ledger_revision"] == "ledger-2"
    json.dumps(r, allow_nan=False)
    assert not any(k in json.dumps(r) for k in ('"order_generated"', '"execution_permission"'))
    assert "synthetic_tests_do_not_prove_real_forward_samples_available" in r["limitations"]


@pytest.mark.parametrize("source", ["09:45:50", "09:45:51", "09:45:57", "09:45:58"])
def test_real_counterexample_new_observed_old_source_is_not_post_trim(source):
    c = fixture_case()
    c["frames"] = [frame(c, source, 9, observed=clock("09:45:59"))]
    r = rows(c)[0]
    assert r["candidate"] == "unknown"
    assert r["reason"] == "source_not_strictly_after_confirmation"
    assert r["source_after_confirmation_seconds"] <= 0


def test_duplicate_is_not_second_sample_but_later_new_frame_recovers():
    c = normal()
    duplicate = deepcopy(c["frames"][0])
    duplicate["observed_at"] = duplicate["received_at"] = clock("09:46:20")
    duplicate["original_gate"]["known_at"] = duplicate["observed_at"]
    c["frames"].insert(1, duplicate)
    r = rows(c)
    assert r[1]["reason"] == "same_source_duplicate_not_new_sample"
    assert r[2]["candidate"] == "research_condition_met"


@pytest.mark.parametrize("kind", ["conflict", "round", "rollback", "unknown"])
def test_bad_frame_breaks_segment_and_full_new_segment_can_recover(kind):
    c = fixture_case()
    seed = frame(c)
    if kind == "conflict":
        bad = frame(c, price=9, observed=clock("09:46:10"))
    elif kind == "round":
        bad = frame(c, "09:46:05", 9, round_id=seed["round_id"])
    elif kind == "rollback":
        bad = frame(c, "09:45:59", 9, observed=clock("09:46:10"))
    else:
        bad = frame(c, "09:46:05", 9)
        bad["corporate_action_status"] = None
    c["frames"] = [seed, bad, frame(c, "09:46:30", 9.5), frame(c, "09:47:00", 9)]
    r = rows(c)
    assert r[1]["candidate"] == "unknown"
    assert r[2]["candidate"] == "waiting"
    assert r[3]["candidate"] == "research_condition_met"


@pytest.mark.parametrize("gap", ["09:50:00", "13:00:00"])
def test_gap_and_lunch_start_new_segment(gap):
    c = fixture_case()
    end = (datetime.fromisoformat(clock(gap)) + timedelta(seconds=30)).time().isoformat()
    c["frames"] = [frame(c), frame(c, gap, 9), frame(c, end, 8.8)]
    r = rows(c)
    assert r[1]["reason"] == "new_segment_seed_after_gap"
    assert r[2]["candidate"] == "research_condition_met"


@pytest.mark.parametrize("key,value", [
    ("cycle_id", "reopened"), ("account_instance", "another-account"), ("code", "600103"),
    ("buy_time", "2026-09-08T10:00:00"), ("entry_version", "entry-v2")])
def test_identity_changes_invalidate_anchor_permanently(key, value):
    c = normal()
    bad = deepcopy(c["frames"][0])
    bad["identity"][key] = value
    c["frames"].insert(1, bad)
    assert rows(c)[-1]["reason"] == "anchor_invalidated_by_identity_revision_quantity_or_close"


@pytest.mark.parametrize("key,value", [
    ("revision", "ledger-3"), ("remaining_quantity", 200), ("remaining_quantity", 0),
    ("remaining_quantity", True), ("position_active", False)])
def test_later_trim_add_or_close_cannot_reuse_old_anchor(key, value):
    c = normal()
    bad = deepcopy(c["frames"][0])
    bad[key] = value
    c["frames"].insert(1, bad)
    assert rows(c)[1]["candidate"] == "unknown"
    if type(value) is not bool or key == "position_active":
        assert rows(c)[-1]["candidate"] == "unknown"


@pytest.mark.parametrize("hard", ["hard_stop", "max_hold", "frozen_forced_exit"])
def test_proven_hard_intent_preserved_with_unknown_anchor_and_original_blocks(hard):
    c = normal()
    c["anchor"]["commit_known_at"] = None
    c["frames"][0]["price"] = None
    g = c["frames"][0]["original_gate"]
    g.update(exit_class=hard, hard_proof_ref="hard-proof-1")
    if hard == "frozen_forced_exit":
        g["matched_forced_rule_id"] = "forced-rule-1"
        g["frozen_rules"]["forced_exit_rule_ids"] = ["forced-rule-1"]
        g["rules_content_hash"] = _hash(g["frozen_rules"])
    g["checks"]["orderbook"]["passed"] = False
    r = rows(c)[0]
    assert r["candidate"] == "preserve_original_hard_exit"
    assert r["original_gate_evidence"]["checks"]["orderbook"]["passed"] is False


@pytest.mark.parametrize("damage", ["proof", "rules", "missing", "entry"])
def test_hard_reason_text_is_not_frozen_proof(damage):
    c = normal()
    g = c["frames"][0]["original_gate"]
    g.update(exit_class="hard_stop", hard_proof_ref="proof", reason="触发硬止损")
    if damage == "proof":
        del g["hard_proof_ref"]
    elif damage == "rules":
        g["frozen_rules"] = {}
    elif damage == "entry":
        g["entry_version"] = "other"
    else:
        g["missing_keys"] = ["forced_exit_rules"]
    assert rows(c)[0]["candidate"] == "unknown"


@pytest.mark.parametrize("field,value", [
    ("status", "submitted"), ("filled_quantity", 0), ("commit_known_at", None),
    ("before_quantity", 201), ("first_trim_in_cycle", False),
    ("revision_basis", "observed_state_content_hash"), ("after_revision", "ledger-1"),
    ("response_observed_at", clock("09:45:54")),
    ("commit_known_at", clock("15:01:00"))])
def test_unfilled_incomplete_or_conflicting_anchor_keeps_unknown_denominator(field, value):
    c = normal()
    c["anchor"][field] = value
    r = report(c)
    assert r["case_denominator"] == 1 and r["frame_denominator"] == 2
    assert all(f["candidate"] == "unknown" for f in r["results"][0]["frames"])


def test_split_fills_and_duplicate_fill_rejection():
    c = normal()
    f = c["anchor"]["fills"][0]
    f["quantity"] = 50
    other = deepcopy(f)
    other.update(fill_id="fill-2", quantity=50)
    c["anchor"]["fills"].append(other)
    assert rows(c)[-1]["candidate"] == "research_condition_met"
    other["fill_id"] = f["fill_id"]
    assert rows(c)[-1]["reason"] == "fill_identity_or_quantity_invalid"


@pytest.mark.parametrize("before,sold,expected", [
    (100, 100, "unknown"), (200, 100, "research_condition_met"),
    (300, 100, "research_condition_met"), (199, 100, "blocked_by_original_gate")])
def test_100_200_share_boundaries(before, sold, expected):
    c = fixture_case(before, sold)
    c["frames"] = [frame(c), frame(c, "09:46:30", 9)]
    assert rows(c)[-1]["candidate"] == expected


@pytest.mark.parametrize("check", ["t1", "quantity", "quote", "orderbook", "other_original_blocks"])
def test_original_gate_cannot_be_bypassed(check):
    c = normal()
    c["frames"][-1]["original_gate"]["checks"][check]["passed"] = False
    assert rows(c)[-1]["candidate"] == "blocked_by_original_gate"


@pytest.mark.parametrize("key,value", [
    ("price", True), ("price", float("nan")), ("price", float("inf")), ("price", 10**400),
    ("corporate_action_status", None), ("price_basis", "adjusted"), ("source", "other"),
    ("sellable_quantity", None), ("sellable_quantity", 200),
    ("source_quote_at", clock("15:01:00")), ("observed_at", clock("15:01:00")),
    ("received_at", clock("09:45:59")), ("revision_known_at", None)])
def test_bad_price_quantity_basis_or_clock_is_unknown(key, value):
    c = normal()
    c["frames"][-1][key] = value
    r = rows(c)[-1]
    assert r["candidate"] == "unknown"
    json.dumps(report(c), allow_nan=False)


def test_calendar_not_guessed_from_weekdays_and_t1_is_independent():
    c = normal()
    assert rows(c, cal=[])[-1]["reason"] == "calendar_unknown"
    c["identity"]["buy_time"] = clock("09:30:00")
    c["anchor"]["before_revision_known_at"] = c["identity"]["buy_time"]
    c["anchor"]["identity"] = deepcopy(c["identity"])
    c["anchor"]["fills"][0]["identity"] = deepcopy(c["identity"])
    c["frames"] = [frame(c), frame(c, "09:46:30", 9)]
    assert rows(c)[-1]["reason"] == "t_plus_one"


@pytest.mark.parametrize("value", [0, -1, True, float("nan"), float("inf"), 10**400, 100])
def test_policy_rejects_nonfinite_nonpositive_and_bool(value):
    with pytest.raises(ValueError):
        PostTrimPolicy("research:test", value, 90, 90)


def test_tiny_threshold_does_not_swallow_zero_and_decimal_boundary():
    c = normal()
    c["frames"][-1] = frame(c, "09:46:30", 10)
    assert rows(c, policy=PostTrimPolicy("research:tiny", 1e-12, 90, 90))[-1]["candidate"] == "waiting"
    c["frames"][-1] = frame(c, "09:46:30", 9.9)
    assert rows(c)[-1]["candidate"] == "research_condition_met"


def test_derived_nonfinite_breaks_then_recovers_and_no_daily_high_used():
    c = fixture_case()
    c["frames"] = [frame(c, price=1e-308), frame(c, "09:46:30", 1e308),
                   frame(c, "09:47:00", 10), frame(c, "09:47:30", 9)]
    for f in c["frames"]:
        f["session_high"] = 1e300
    r = rows(c)
    assert r[1]["reason"] == "derived_profit_nonfinite"
    assert r[2]["candidate"] == "waiting"
    assert r[3]["candidate"] == "research_condition_met"
    json.dumps(report(c), allow_nan=False)


def test_nonhard_trigger_required_and_missing_cases_keep_denominator():
    c = normal()
    c["frames"][-1]["original_gate"]["triggered"] = False
    r = rows(c)[-1]
    assert r["candidate"] == "not_applicable" and r["deterioration_pct"] is None
    r = build_post_trim_report([{}, None], [], as_of=clock("15:00:00"),
                              policy=PostTrimPolicy("research:test", 1, 90, 90))
    assert r["case_denominator"] == 2 and r["frame_denominator"] == 2


@pytest.mark.parametrize("missing", ["identity", "revision", "remaining_quantity", "position_active"])
def test_unknown_state_not_livelock_and_cannot_inherit_old_segment(missing):
    c = fixture_case()
    bad = frame(c, "09:46:10", 9)
    del bad[missing]
    c["frames"] = [frame(c), bad, frame(c, "09:46:30", 9), frame(c, "09:47:00", 8)]
    r = rows(c)
    assert r[1]["candidate"] == "unknown"
    assert r[2]["candidate"] == "waiting"
    assert r[3]["candidate"] == "research_condition_met"


@pytest.mark.parametrize("field", ["quote_content_hash", "evaluated_quantity", "execution_price"])
def test_original_execution_binding_cannot_be_just_passed_flags(field):
    c = normal()
    del c["frames"][-1]["original_gate"][field]
    assert rows(c)[-1]["reason"] == "original_execution_quantity_quote_or_clock_binding_unknown"


def test_duplicate_case_identity_preserves_unknown_not_double_coverage():
    c = normal()
    r = build_post_trim_report([c, deepcopy(c)], calendar(), as_of=clock("15:00:00"),
                              policy=PostTrimPolicy("research:test", 1, 90, 90))
    assert r["case_denominator"] == 2 and r["anchor_unknown_case_count"] == 2
    assert r["candidate_counts"] == {"unknown": 4}


def test_source_watermark_survives_rollback_without_livelock():
    c = fixture_case()
    c["frames"] = [frame(c, "09:47:00", 10),
                   frame(c, "09:46:00", 9, observed=clock("09:47:01")),
                   frame(c, "09:46:30", 8, observed=clock("09:47:02")),
                   frame(c, "09:47:30", 10), frame(c, "09:48:00", 9)]
    r = rows(c)
    assert r[1]["reason"] == r[2]["reason"] == "source_rollback"
    assert r[3]["candidate"] == "waiting"
    assert r[4]["candidate"] == "research_condition_met"


@pytest.mark.parametrize("field", ["basis_known_at", "before_revision_known_at"])
def test_anchor_first_knowledge_not_backfilled(field):
    c = normal()
    c["anchor"][field] = clock("09:50:00")
    assert rows(c)[-1]["candidate"] == "unknown"


def test_split_below_100_first_trim_not_original_post_t_context():
    c = fixture_case(200, 50)
    c["frames"] = [frame(c), frame(c, "09:46:30", 9)]
    assert rows(c)[-1]["reason"] == "original_same_day_trim_100_share_context_unproven"


def test_hard_frame_revision_change_still_retires_old_anchor():
    c = normal()
    hard = deepcopy(c["frames"][0])
    hard["revision"] = hard["original_gate"]["revision"] = "ledger-3"
    hard["original_gate"].update(exit_class="hard_stop", hard_proof_ref="proof")
    c["frames"].insert(1, hard)
    r = rows(c)
    assert r[1]["candidate"] == "preserve_original_hard_exit"
    assert r[2]["reason"] == "anchor_invalidated_by_identity_revision_quantity_or_close"


@pytest.mark.parametrize("key", ["entry_policy_revision", "entry_policy_ref", "entry_rules_content_hash"])
def test_frozen_policy_binding_cannot_change_between_anchor_and_frames(key):
    c = normal()
    c["anchor"][key] = "different"
    assert rows(c)[-1]["reason"] == "frozen_entry_policy_binding_unknown"


def test_ignored_duplicate_still_consumes_its_round():
    c = fixture_case()
    c["frames"] = [frame(c), frame(c, round_id="reused", observed=clock("09:46:10")),
                   frame(c, "09:46:30", 9, round_id="reused"),
                   frame(c, "09:47:00", 9), frame(c, "09:47:30", 8)]
    r = rows(c)
    assert r[1]["reason"] == "same_source_duplicate_not_new_sample"
    assert r[2]["reason"] == "same_round_reuse"
    assert r[3]["candidate"] == "waiting"
    assert r[4]["candidate"] == "research_condition_met"


@pytest.mark.parametrize("damage", ["basis", "gate"])
def test_negative_frame_source_boundary_requires_truly_new_recovery(damage):
    c = fixture_case()
    bad = frame(c, "09:47:00", 10)
    if damage == "basis":
        bad["corporate_action_status"] = None
    else:
        bad["original_gate"] = None
    c["frames"] = [frame(c), bad,
                   frame(c, "09:46:30", 9, observed=clock("09:47:02")),
                   frame(c, "09:46:50", 8, observed=clock("09:47:03")),
                   frame(c, "09:47:00", 7, observed=clock("09:47:04")),
                   frame(c, "09:47:30", 10), frame(c, "09:48:00", 9)]
    r = rows(c)
    assert all(f["candidate"] == "unknown" for f in r[2:5])
    assert r[5]["candidate"] == "waiting"
    assert r[6]["candidate"] == "research_condition_met"


def test_hard_frame_updates_observation_watermark_before_preserving_intent():
    c = fixture_case()
    hard = frame(c, "09:47:00", 10, observed=clock("09:47:10"))
    hard["original_gate"].update(exit_class="hard_stop", hard_proof_ref="proof")
    c["frames"] = [frame(c), hard,
                   frame(c, "09:47:01", 9, observed=clock("09:47:02")),
                   frame(c, "09:47:05", 8, observed=clock("09:47:06")),
                   frame(c, "09:47:30", 10), frame(c, "09:48:00", 9)]
    r = rows(c)
    assert r[1]["candidate"] == "preserve_original_hard_exit"
    assert r[2]["reason"] == r[3]["reason"] == "observation_order_rollback"
    assert r[4]["candidate"] == "waiting"
    assert r[5]["candidate"] == "research_condition_met"


@pytest.mark.parametrize("damage", ["future_observation", "future_source", "bad_received", "bad_source"])
def test_invalid_clocks_cannot_raise_source_recovery_boundary(damage):
    c = fixture_case()
    bad = frame(c, "09:50:00", observed=clock("09:47:01"))
    if damage == "future_observation":
        bad["observed_at"] = clock("15:01:00")
    elif damage == "bad_received":
        bad["source_quote_at"] = clock("09:47:00")
        bad["received_at"] = "bad"
    elif damage == "bad_source":
        bad["source_quote_at"] = "bad"
    c["frames"] = [frame(c), bad,
                   frame(c, "09:46:30", 10, observed=clock("09:47:02")),
                   frame(c, "09:46:50", 9, observed=clock("09:47:03"))]
    r = rows(c)
    assert r[1]["candidate"] == "unknown"
    assert r[2]["candidate"] == "waiting"
    assert r[3]["candidate"] == "research_condition_met"


def test_hard_rollback_preserves_intent_without_lowering_observation_watermark():
    c = fixture_case()
    hard = frame(c, "09:46:30", observed=clock("09:46:31"))
    hard["original_gate"].update(exit_class="hard_stop", hard_proof_ref="proof")
    c["frames"] = [frame(c, "09:47:00"), hard,
                   frame(c, "09:46:50", observed=clock("09:46:51")),
                   frame(c, "09:47:30"), frame(c, "09:48:00", 9)]
    r = rows(c)
    assert r[1]["candidate"] == "preserve_original_hard_exit"
    assert r[1]["observation_order_rollback"] is True
    assert r[2]["reason"] == "observation_order_rollback"
    assert r[3]["candidate"] == "waiting"
    assert r[4]["candidate"] == "research_condition_met"


@pytest.mark.parametrize("args", [
    ("production", 1, 90, 90), ("research:test", 1, True, 90),
    ("research:test", 1, 90, 0), ("research:test", 1, 90, 90, "fraction")])
def test_policy_units_version_and_time_limits_are_explicit(args):
    with pytest.raises(ValueError):
        PostTrimPolicy(*args)
