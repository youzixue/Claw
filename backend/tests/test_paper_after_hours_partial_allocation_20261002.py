"""Remaining-aware proposals ONLY; simulated state/priors are not actual receipts."""
from copy import deepcopy
from datetime import timedelta
import json

import pytest

from app.trading import paper_after_hours_allocation as allocation
from test_paper_after_hours_allocation_20261002 import (
    AT, NOW, feed, external, local, rehash, traded_feed, audited_fixture,
)


def partial_plan(payload=None, orders=None, **kwargs):
    return allocation.propose_partial_allocations(
        feed() if payload is None else payload, [local()] if orders is None else orders,
        now=kwargs.pop("now", NOW), scenario_account="default", **kwargs)


def capacity(shares):
    payload = feed()
    payload["initial"]["orders"][0]["quantity"] = shares
    return payload


def append_offer(payload, *, identifier, shares, at, frame):
    result = deepcopy(payload)
    sequence = result["last_sequence"] + 1
    result["events"].append({"record_id": "add." + identifier, "sequence": sequence,
        "source_at": at.isoformat(), "kind": "add",
        "order": external(identifier, quantity=shares, sequence=sequence, accepted=at)})
    result.update(last_sequence=sequence, frame_id=frame, source_quote_at=at.isoformat(),
                  received_at=(at + timedelta(milliseconds=100)).isoformat(),
                  available_at=(at + timedelta(milliseconds=200)).isoformat())
    return result


def simulated_state(order, filled, *, canceled=False):
    # No book, fee, cash or execution receipt is created by this test helper.
    result = deepcopy(order)
    result.filled_quantity = filled
    result.status = "canceled" if canceled else (
        "filled" if filled == result.quantity else "partial" if filled else "submitted")
    if canceled:
        risk = json.loads(result.risk_json)
        risk["paper_after_hours_cancel"] = {
            "contract_version": allocation.PARTIAL_CANCEL_OBSERVATION_PROTOCOL,
            "order_id": result.order_id, "original_quantity": result.quantity,
            "filled_quantity_preserved": filled, "unfilled_quantity": result.quantity - filled,
            "canceled_at": (NOW+timedelta(milliseconds=200)).isoformat(),
            "simulation_only": True, "exchange_cancel_receipt": None,
        }
        result.risk_json = json.dumps(risk)
    return result


def assert_not_executable(result):
    assert result["fills"] == []
    assert result["execution_authorized"] is result["fillable"] is False
    assert result["partial_fill_allowed"] is result["ledger_contract_supported"] is False


def test_production_sources_still_cannot_make_even_partial_plans():
    payload = feed()
    for source, profile in allocation._PROVIDERS.items():
        payload.update(source=source, source_version=profile.version,
                       partial_fill_allowed=True, capability="order_level",
                       execution_authorized=True, queue_verified=True)
        result = partial_plan(payload)
        assert result["proposals"] == [] and result["reason"] == "provider_aggregate_only"
        assert_not_executable(result)
    assert allocation.matching_capabilities()["order_level_provider_count"] == 0


def test_head_gets_fragment_but_smaller_order_never_jumps_remainder(audited_fixture):
    payload = capacity(300)
    orders = [local("later", sequence=2), local("head", quantity=400)]
    before = deepcopy(payload), deepcopy([o.__dict__ for o in orders])
    result = partial_plan(payload, orders)
    assert result["contract_version"] == allocation.PARTIAL_ALLOCATION_PROTOCOL
    assert result["status"] == "proposal_only"
    assert [(p["order_id"], p["quantity"], p["remaining_quantity_after"])
            for p in result["proposals"]] == [("head", 300, 100)]
    assert [w["order_id"] for w in result["waiting"]] == ["head", "later"]
    assert result["proposals"][0]["fixed_price"] == 10  # Original limit remains 10.5.
    assert_not_executable(result)
    assert before == (payload, [o.__dict__ for o in orders])
    assert result == partial_plan(payload, orders)
    # The old version remains strictly full-fill, with unchanged queue semantics.
    old = allocation.propose_allocations(payload, orders, now=NOW, scenario_account="default")
    assert old["proposals"] == [] and old["partial_fill_allowed"] is False


def test_native_odd_slices_aggregate_into_lot_fragment_without_consuming_dust(audited_fixture):
    payload = feed()
    payload["initial"]["orders"] = [external("a", quantity=70),
                                     external("b", quantity=180, sequence=2)]
    payload["initial"]["sequence"] = payload["last_sequence"] = 2
    result = partial_plan(payload, [local(quantity=300)])
    fragment = result["proposals"][0]
    assert fragment["quantity"] == 200 and fragment["remaining_quantity_after"] == 100
    assert [(r["external_order_id"], r["offset"], r["shares"])
            for r in fragment["resources"]] == [("a", 0, 70), ("b", 0, 130)]
    assert sum(r["shares"] for r in fragment["resources"]) == fragment["quantity"]
    assert_not_executable(result)


@pytest.mark.parametrize("shares", [1, 50, 99])
def test_sub_lot_capacity_is_not_fabricated_into_a_fragment(audited_fixture, shares):
    result = partial_plan(capacity(shares))
    assert result["status"] == "waiting" and result["proposals"] == []
    assert result["waiting"][0]["reason"] == "insufficient_unconsumed_order_level_counterparty"
    assert_not_executable(result)


def test_three_fragments_preserve_original_fifo_and_immutable_lifecycle(audited_fixture):
    original = local("head", quantity=300)
    later = local("later", sequence=2)
    payload = capacity(150)
    first = partial_plan(payload, [original, later])["proposals"][0]
    assert (first["fragment_index"], first["cumulative_before"], first["cumulative_after"]) == (1, 0, 100)
    head = simulated_state(original, 100)
    second_feed = append_offer(payload, identifier="offer.2", shares=100, at=AT+timedelta(seconds=2), frame="frame.2")
    second_result = partial_plan(second_feed, [later, head], now=AT+timedelta(seconds=3),
                                 prior_proposals=[first])
    second = second_result["proposals"][0]
    assert second["order_id"] == original.order_id
    assert (second["fragment_index"], second["cumulative_before"], second["cumulative_after"]) == (2, 100, 200)
    assert second["local_acceptance_sequence"] == first["local_acceptance_sequence"]
    assert second["local_accepted_at"] == first["local_accepted_at"]
    assert second["local_intent_hash"] == first["local_intent_hash"]
    assert [(r["external_order_id"], r["offset"], r["shares"])
            for r in second["resources"]] == [("offer", 100, 50), ("offer.2", 0, 50)]
    head = simulated_state(original, 200)
    third_feed = append_offer(second_feed, identifier="offer.3", shares=100,
                              at=AT+timedelta(seconds=4), frame="frame.3")
    third_result = partial_plan(third_feed, [head, later], now=AT+timedelta(seconds=5),
                                prior_proposals=[first, second])
    third = third_result["proposals"][0]
    assert (third["fragment_index"], third["cumulative_before"], third["cumulative_after"],
            third["remaining_quantity_after"]) == (3, 200, 300, 0)
    assert sum(p["quantity"] for p in [first, second, third]) == original.quantity
    done = simulated_state(original, 300)
    replay = partial_plan(third_feed, [done, later], now=AT+timedelta(seconds=5),
                           prior_proposals=[first, second, third])
    assert replay["proposals"] == []  # Only 50 untouched native shares remain.
    assert_not_executable(replay)


def test_canceling_partial_state_preserves_old_slices_not_returning_capacity(audited_fixture):
    payload, original = capacity(150), local("head", quantity=300)
    first = partial_plan(payload, [original])["proposals"][0]
    later = local("later", sequence=2)
    canceled = simulated_state(original, 100, canceled=True)
    assert partial_plan(payload, [canceled, later], now=NOW+timedelta(seconds=1),
                        prior_proposals=[first])["proposals"] == []
    more = append_offer(payload, identifier="new", shares=100, at=AT+timedelta(seconds=2), frame="new.frame")
    result = partial_plan(more, [canceled, later], now=AT+timedelta(seconds=3), prior_proposals=[first])
    proposal = result["proposals"][0]
    assert proposal["order_id"] == later.order_id
    assert [(r["external_order_id"], r["offset"], r["shares"]) for r in proposal["resources"]] == [
        ("offer", 100, 50), ("new", 0, 50)]
    assert canceled.filled_quantity == 100  # No actual cancellation/fee mutation is performed.
    assert_not_executable(result)


@pytest.mark.parametrize("status,filled", [
    ("partial", 0), ("partial", 50), ("partial", True), ("partial", 100.0),
    ("partial", -100), ("partial", 400), ("partial", 300), ("submitted", 100),
    ("filled", 100), ("canceled", 300), ("deferred", 0),
])
def test_invalid_local_fragment_state_is_blocked_not_upgraded(audited_fixture, status, filled):
    order = local(quantity=300)
    order.status, order.filled_quantity = status, filled
    result = partial_plan(orders=[order])
    assert result["status"] == "evidence_blocked" and result["proposals"] == []
    assert_not_executable(result)


def test_claimed_fill_without_complete_prior_history_is_not_trusted(audited_fixture):
    original = local(quantity=200)
    prior = partial_plan(capacity(150), [original])["proposals"][0]
    partial = simulated_state(original, 100)
    assert partial_plan(orders=[partial])["status"] == "evidence_blocked"
    assert partial_plan(orders=[original], prior_proposals=[prior])["status"] == "evidence_blocked"
    assert partial_plan(orders=[local("later", sequence=2)], prior_proposals=[prior])["status"] == "evidence_blocked"
    done = simulated_state(local(quantity=100), 100)
    assert partial_plan(orders=[done])["status"] == "evidence_blocked"


@pytest.mark.parametrize("change", [
    lambda p: p.update(fragment_index=2),
    lambda p: p.update(cumulative_before=100),
    lambda p: p.update(cumulative_after=200),
    lambda p: p.update(remaining_quantity_after=0),
    lambda p: p.update(original_quantity=400),
    lambda p: p.update(original_quantity=300.0),
    lambda p: p.update(local_acceptance_sequence=True),
    lambda p: p.update(local_intent_hash="0"*64),
    lambda p: p.update(original_limit_price=True),
    lambda p: p.update(execution_authorized=True),
    lambda p: p["resources"][0].update(offset=100),
])
def test_rehashed_partial_history_must_still_match_identity_chain_and_slices(audited_fixture, change):
    original = local(quantity=300)
    payload = capacity(150)
    prior = partial_plan(payload, [original])["proposals"][0]
    change(prior)
    rehash(prior)
    result = partial_plan(payload, [simulated_state(original, 100)], prior_proposals=[prior])
    assert result["status"] == "evidence_blocked" and result["proposals"] == []
    assert_not_executable(result)


def test_missing_duplicate_or_out_of_order_fragment_cannot_reconcile(audited_fixture):
    original = local(quantity=300)
    first_feed = capacity(150)
    first = partial_plan(first_feed, [original])["proposals"][0]
    second_feed = append_offer(first_feed, identifier="new", shares=100,
                               at=AT+timedelta(seconds=2), frame="new.frame")
    second = partial_plan(second_feed, [simulated_state(original, 100)], now=AT+timedelta(seconds=3),
                          prior_proposals=[first])["proposals"][0]
    head = simulated_state(original, 200)
    for priors in ([first], [second], [second, first], [first, first], [first, second, second]):
        result = partial_plan(second_feed, [head], now=AT+timedelta(seconds=3), prior_proposals=priors)
        assert result["status"] == "evidence_blocked" and result["proposals"] == []


def test_revised_seen_trade_or_old_frame_cannot_resurrect_fragment_resources(audited_fixture):
    original = local(quantity=400)
    payload = traded_feed(100)
    payload["initial"]["orders"][0]["quantity"] = 350
    first = partial_plan(payload, [original])["proposals"][0]
    assert first["resources"][0]["offset"] == 100 and first["quantity"] == 200
    revision = traded_feed(50)
    revision["initial"]["orders"][0]["quantity"] = 350
    revision["frame_id"] = "revised"
    order = simulated_state(original, 200)
    for bad in (revision, capacity(350)):
        result = partial_plan(bad, [order], prior_proposals=[first])
        assert result["status"] == "evidence_blocked" and result["proposals"] == []


@pytest.mark.parametrize("now", [AT.replace(minute=30), AT+timedelta(seconds=15),
                                AT+timedelta(days=1), AT-timedelta(seconds=1)])
def test_partial_mode_does_not_extend_source_or_session_clocks(audited_fixture, now):
    result = partial_plan(now=now)
    assert result["proposals"] == [] and result["fills"] == []


def test_unknown_external_predecessors_never_become_zero(audited_fixture):
    payload = feed()
    payload["initial"]["orders"] = [external("ahead", side="buy")]
    result = partial_plan(payload, [local(quantity=300), local("later", sequence=2)])
    assert result["proposals"] == []
    assert result["waiting"][0]["verified_external_ahead_shares"] == 300
    assert result["waiting"][1]["reason"] == "earlier_local_head_not_fully_allocatable"


def test_no_local_account_order_becomes_a_mutual_counterparty(audited_fixture):
    payload = feed()
    payload["initial"]["orders"] = []
    result = partial_plan(payload, [local(quantity=300), local("sell", sequence=2, side="sell", limit=9.5)])
    assert result["proposals"] == [] and result["fills"] == []
    assert partial_plan(orders=[local(account="promotion")])["status"] == "evidence_blocked"


def test_protocol_versions_cannot_be_interchanged_for_replay_or_frozen_fill(audited_fixture):
    from test_paper_after_hours_contract_20261002 import original as full_original, frozen
    original = local(quantity=300)
    fragment = partial_plan(capacity(150), [original])["proposals"][0]
    full = allocation.propose_allocations(feed(), [local()], now=NOW, scenario_account="default")["proposals"][0]
    assert partial_plan(orders=[local()], prior_proposals=[full])["status"] == "evidence_blocked"
    assert allocation.propose_allocations(feed(), [original], now=NOW, scenario_account="default",
                                          prior_proposals=[fragment])["status"] == "evidence_blocked"
    with pytest.raises(ValueError, match="fixed_price_candidate_resources_unavailable"):
        frozen(order=full_original(), prior_proposals=[fragment])


@pytest.mark.parametrize("relabel_prefix", [False, True])
def test_fragment_cannot_claim_resources_that_did_not_exist_in_its_source_prefix(audited_fixture, relabel_prefix):
    original = local(quantity=300)
    payload = capacity(150)
    first = partial_plan(payload, [original])["proposals"][0]
    next_feed = append_offer(payload, identifier="new", shares=100,
                             at=AT+timedelta(seconds=2), frame="frame.2")
    second = partial_plan(next_feed, [simulated_state(original, 100)], now=AT+timedelta(seconds=3),
                           prior_proposals=[first])["proposals"][0]
    for key in ("source_quote_at", "source_available_at"):
        second[key] = first[key]
    if relabel_prefix:
        # Full metadata cloning still cannot make the new native order exist in seq=1.
        for key in ("frame_id", "source_evidence_hash", "terminal_sequence", "lifecycle_prefix_hash",
                    "source_expires_at"):
            second[key] = first[key]
    rehash(second)
    result = partial_plan(next_feed, [simulated_state(original, 200)], now=AT+timedelta(seconds=3),
                           prior_proposals=[first, second])
    assert result["status"] == "evidence_blocked" and result["proposals"] == []


def test_prior_fragment_cannot_precede_local_terminal_validation_even_when_rehashed(audited_fixture):
    original = local(quantity=300)
    prior = partial_plan(capacity(150), [original])["proposals"][0]
    changed = simulated_state(original, 100)
    proof = json.loads(changed.risk_json)
    proof["paper_after_hours_intent"]["terminal_validated_at"] = (NOW+timedelta(seconds=1)).isoformat()
    changed.risk_json = json.dumps(proof)
    prior["local_intent_hash"] = allocation._hash(proof["paper_after_hours_intent"])
    rehash(prior)
    result = partial_plan(capacity(150), [changed], now=NOW+timedelta(seconds=2), prior_proposals=[prior])
    assert result["status"] == "evidence_blocked" and result["proposals"] == []


def test_proven_invalid_limit_head_does_not_block_valid_partial_later_order(audited_fixture):
    orders = [local("bad.head", quantity=300, limit=9), local("good.later", sequence=2, quantity=200)]
    result = partial_plan(capacity(150), orders)
    assert result["waiting"][0]["reason"] == "original_limit_incompatible_with_official_close"
    assert result["proposals"][0]["order_id"] == "good.later"
    assert result["proposals"][0]["quantity"] == 100


def test_past_trade_interval_cannot_be_reclaimed_by_a_rehashed_fragment(audited_fixture):
    original = local(quantity=400)
    payload = traded_feed(100)
    payload["initial"]["orders"][0]["quantity"] = 350
    prior = partial_plan(payload, [original])["proposals"][0]
    assert prior["quantity"] == 200 and prior["resources"][0]["offset"] == 100
    legal = partial_plan(payload, [simulated_state(original, 200)], prior_proposals=[prior])
    assert legal["status"] == "waiting"  # Only untouched 300..350 remains.
    prior["resources"][0]["offset"] = 0
    rehash(prior)
    result = partial_plan(payload, [simulated_state(original, 200)], prior_proposals=[prior])
    assert result["status"] == "evidence_blocked" and result["proposals"] == []


def test_later_real_trade_does_not_invalidate_a_valid_earlier_fragment(audited_fixture):
    original = local(quantity=400)
    prior = partial_plan(feed(), [original])["proposals"][0]  # 0..300 was live at prefix 1.
    assert prior["quantity"] == 300
    later = traded_feed(100)
    later.update(frame_id="real.trade.after.plan")
    # Move the real events after the prior clock, retaining a complete new prefix.
    at = AT+timedelta(seconds=2)
    for event in later["events"]:
        event["source_at"] = at.isoformat()
        if event["kind"] == "add":
            event["order"]["accepted_at"] = at.isoformat()
    later.update(source_quote_at=at.isoformat(),
                 received_at=(at+timedelta(milliseconds=100)).isoformat(),
                 available_at=(at+timedelta(milliseconds=200)).isoformat())
    result = partial_plan(later, [simulated_state(original, 300)], now=AT+timedelta(seconds=3),
                           prior_proposals=[prior])
    assert result["status"] == "waiting"  # Union of real filled prefix and old slices; no release.


def test_prior_after_native_cancel_is_not_a_live_resource_slice(audited_fixture):
    original = local(quantity=400)
    payload = capacity(150)
    prior = partial_plan(payload, [original])["proposals"][0]
    at = AT+timedelta(seconds=2)
    canceled_feed = deepcopy(payload)
    canceled_feed["events"] = [{"record_id": "cancel.2", "sequence": 2, "source_at": at.isoformat(),
                               "kind": "cancel", "order_id": "offer", "quantity": 150}]
    canceled_feed.update(last_sequence=2, frame_id="canceled.frame", source_quote_at=at.isoformat(),
                         received_at=(at+timedelta(milliseconds=100)).isoformat(),
                         available_at=(at+timedelta(milliseconds=200)).isoformat())
    assert partial_plan(canceled_feed, [simulated_state(original, 100)], now=AT+timedelta(seconds=3),
                        prior_proposals=[prior])["status"] == "waiting"
    snapshot = allocation._verify(canceled_feed, now=AT+timedelta(seconds=3))
    prior.update(frame_id=snapshot.frame_id, source_evidence_hash=snapshot.evidence_hash,
                 source_quote_at=snapshot.source_at.isoformat(), source_available_at=snapshot.available_at.isoformat(),
                 terminal_sequence=snapshot.terminal_sequence,
                 lifecycle_prefix_hash=snapshot.lifecycle_prefixes[snapshot.terminal_sequence],
                 proposed_at=(AT+timedelta(seconds=3)).isoformat(),
                 source_expires_at=snapshot.expires_at.isoformat())
    rehash(prior)
    result = partial_plan(canceled_feed, [simulated_state(original, 100)], now=AT+timedelta(seconds=3),
                           prior_proposals=[prior])
    assert result["status"] == "evidence_blocked"


def test_history_fragment_cannot_shrink_head_and_claim_later_fill_around_remainder(audited_fixture):
    head, later = local("head", quantity=300), local("later", sequence=2)
    payload = capacity(400)
    first, second = partial_plan(payload, [head, later])["proposals"]
    first.update(quantity=200, cumulative_after=200, remaining_quantity_after=100)
    first["resources"][0]["shares"] = 200
    rehash(first)
    result = partial_plan(payload, [simulated_state(head, 200), simulated_state(later, 100)],
                           prior_proposals=[first, second])
    assert result["status"] == "evidence_blocked" and result["proposals"] == []


def test_cancel_clock_allows_legitimate_later_history_but_does_not_release_old_slices(audited_fixture):
    original, later = local("head", quantity=300), local("later", sequence=2)
    payload = capacity(150)
    prior = partial_plan(payload, [original])["proposals"][0]
    canceled = simulated_state(original, 100, canceled=True)
    more = append_offer(payload, identifier="new", shares=100, at=AT+timedelta(seconds=2), frame="new.frame")
    second = partial_plan(more, [canceled, later], now=AT+timedelta(seconds=3),
                          prior_proposals=[prior])["proposals"][0]
    replay = partial_plan(more, [canceled, simulated_state(later, 100)], now=AT+timedelta(seconds=3),
                          prior_proposals=[prior, second])
    assert replay["status"] == "waiting" and replay["proposals"] == []
    bad = deepcopy(canceled)
    risk = json.loads(bad.risk_json)
    risk["paper_after_hours_cancel"]["canceled_at"] = (AT-timedelta(seconds=1)).isoformat()
    bad.risk_json = json.dumps(risk)
    assert partial_plan(more, [bad, later], now=AT+timedelta(seconds=3), prior_proposals=[prior])["status"] == "evidence_blocked"


@pytest.mark.parametrize("field,value", [
    ("original_quantity", 100.0), ("original_quantity", True), ("original_limit_price", True),
])
def test_partial_original_intent_rejects_same_value_wrong_numeric_type(audited_fixture, field, value):
    payload, order = feed(), local()
    if field == "original_limit_price":
        payload["initial"]["orders"] = [external(side="buy")]
        order = local(side="sell", limit=1)
    risk = json.loads(order.risk_json)
    risk["paper_after_hours_intent"][field] = value
    order.risk_json = json.dumps(risk)
    result = partial_plan(payload, [order])
    assert result["status"] == "evidence_blocked" and result["proposals"] == []


@pytest.mark.parametrize("delay", [15, 20])
def test_rehashed_prior_cannot_claim_planning_after_its_original_source_expiry(audited_fixture, delay):
    original, payload = local(quantity=300), capacity(150)
    prior = partial_plan(payload, [original])["proposals"][0]
    prior["proposed_at"] = (AT+timedelta(seconds=delay)).isoformat()
    rehash(prior)
    fresh = deepcopy(payload)
    at = AT+timedelta(seconds=delay)
    fresh.update(frame_id="fresh", source_quote_at=at.isoformat(),
                 received_at=(at+timedelta(milliseconds=100)).isoformat(),
                 available_at=(at+timedelta(milliseconds=200)).isoformat())
    result = partial_plan(fresh, [simulated_state(original, 100)], now=at+timedelta(seconds=1),
                           prior_proposals=[prior])
    assert result["status"] == "evidence_blocked" and result["proposals"] == []


def test_partial_canceled_state_requires_a_timed_typed_research_witness(audited_fixture):
    original, payload = local(quantity=300), capacity(150)
    prior = partial_plan(payload, [original])["proposals"][0]
    canceled = simulated_state(original, 100, canceled=True)
    risk = json.loads(canceled.risk_json)
    risk.pop("paper_after_hours_cancel")
    canceled.risk_json = json.dumps(risk)
    assert partial_plan(payload, [canceled], now=NOW+timedelta(seconds=1),
                        prior_proposals=[prior])["status"] == "evidence_blocked"


@pytest.mark.parametrize("field,value", [
    ("filled_quantity_preserved", True), ("original_quantity", 300.0),
    ("unfilled_quantity", 100), ("order_id", "other"),
    ("simulation_only", False), ("exchange_cancel_receipt", "not.local.paper.cancel"),
    ("canceled_at", (NOW+timedelta(seconds=5)).isoformat()),
])
def test_partial_cancel_witness_bad_identity_type_or_future_clock_blocks_all(audited_fixture, field, value):
    original, payload = local(quantity=300), capacity(150)
    prior = partial_plan(payload, [original])["proposals"][0]
    canceled = simulated_state(original, 100, canceled=True)
    risk = json.loads(canceled.risk_json)
    risk["paper_after_hours_cancel"][field] = value
    canceled.risk_json = json.dumps(risk)
    result = partial_plan(payload, [canceled], now=NOW+timedelta(seconds=1), prior_proposals=[prior])
    assert result["status"] == "evidence_blocked" and result["proposals"] == []
    assert_not_executable(result)


def test_new_provider_ttl_cannot_extend_old_fragment_frozen_expiry(audited_fixture, monkeypatch):
    original, payload = local(quantity=300), capacity(150)
    prior = partial_plan(payload, [original])["proposals"][0]
    prior["proposed_at"] = (AT+timedelta(seconds=20)).isoformat()
    rehash(prior)
    profile = allocation._PROVIDERS[payload["source"]]
    monkeypatch.setattr(allocation, "_PROVIDERS", {
        payload["source"]: allocation._Provider(profile.version, profile.capability, profile.verification_method, 30)})
    result = partial_plan(payload, [simulated_state(original, 100)], now=AT+timedelta(seconds=21),
                           prior_proposals=[prior])
    assert result["status"] == "evidence_blocked" and result["proposals"] == []


def test_sparse_native_lifecycle_history_is_linear_not_a_book_copy_per_prefix(audited_fixture):
    payload = traded_feed(100)
    payload = append_offer(payload, identifier="new", shares=100, at=AT+timedelta(seconds=2), frame="new.frame")
    snapshot = allocation._verify(payload, now=AT+timedelta(seconds=3))
    # One opening state + two add states + two trade-side updates.
    assert sum(len(states) for states in snapshot.order_lifecycles.values()) == 5
    assert len(snapshot.lifecycle_prefixes) == 4


def test_fragment_conservation_and_replay_for_deterministic_capacity_cases(audited_fixture):
    import random
    generator = random.Random(20261002)
    for _ in range(200):
        shares = generator.randrange(1, 1001)
        originals = [local("head", quantity=100*generator.randrange(1, 6)),
                     local("later", sequence=2, quantity=100*generator.randrange(1, 6))]
        payload = capacity(shares)
        result = partial_plan(payload, list(reversed(originals)))
        proposed = {p["order_id"]: p["quantity"] for p in result["proposals"]}
        total = sum(proposed.values())
        assert total == min(shares//100*100, sum(o.quantity for o in originals))
        slices = [piece for proposal in result["proposals"] for piece in proposal["resources"]]
        intervals = sorted((r["offset"], r["offset"]+r["shares"]) for r in slices)
        assert all(a[1] <= b[0] for a, b in zip(intervals, intervals[1:]))
        assert sum(b-a for a, b in intervals) == total
        if proposed.get("head", 0) < originals[0].quantity:
            assert "later" not in proposed
        states = [simulated_state(order, proposed.get(order.order_id, 0)) for order in originals]
        replay = partial_plan(payload, states, prior_proposals=result["proposals"])
        assert replay["status"] == "waiting" and replay["proposals"] == []
        assert_not_executable(replay)


def test_partial_prior_budget_blocks_without_dropping_history(audited_fixture, monkeypatch):
    original = local(quantity=300)
    prior = partial_plan(capacity(150), [original])["proposals"][0]
    monkeypatch.setattr(allocation, "MAX_LOCAL_ORDERS", 0)
    result = partial_plan(orders=[], prior_proposals=[prior])
    assert result["status"] == "evidence_blocked" and result["proposals"] == []
