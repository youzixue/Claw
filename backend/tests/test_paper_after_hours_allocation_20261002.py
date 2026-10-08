"""Order-level replay fixtures are NOT production feeds, ledger receipts or fills."""
from copy import deepcopy
from datetime import datetime, timedelta
import json
from types import SimpleNamespace

import pytest

from app.data.after_hours import _json
from app.data.sources.after_hours_source import RULE_VERSION, VERSION
from app.trading import paper_after_hours_allocation as allocation
from app.trading.paper_after_hours_execution import CONTRACT, MODE

AT = datetime(2026, 9, 30, 15, 10)
NOW = AT + timedelta(seconds=1)
SOURCE = "isolated.order_level.fixture"


@pytest.fixture
def audited_fixture(monkeypatch):
    # Explicitly test the future schema with a server-owned fixture profile.
    # Neither a public request nor production configuration can add this profile.
    monkeypatch.setattr(allocation, "_PROVIDERS", {
        SOURCE: allocation._Provider("fixture.v1", "order_level", allocation.METHOD),
    })


def external(identifier="offer", *, side="sell", quantity=300, sequence=1,
             accepted=datetime(2026, 9, 30, 9, 30), limit=10):
    return {"record_id": identifier, "side": side, "quantity": quantity, "sequence": sequence,
            "accepted_at": accepted.isoformat(), "limit_price": limit, "unit": "shares"}


def feed():
    return {"protocol": allocation.FEED_PROTOCOL, "source": SOURCE, "source_version": "fixture.v1",
        "verification_method": allocation.METHOD, "session_kind": MODE, "exchange": "SSE",
        "code": "600000", "trade_date": AT.date().isoformat(), "rule_version": RULE_VERSION,
        "security_state_1500": "trading", "session_id": "SSE.600000.2026-09-30", "frame_id": "frame.1",
        "source_quote_at": AT.isoformat(), "received_at": (AT + timedelta(milliseconds=100)).isoformat(),
        "available_at": (AT + timedelta(milliseconds=200)).isoformat(),
        "official_close": {"record_id": "official.close.600000", "code": "600000", "exchange": "SSE",
            "trade_date": AT.date().isoformat(), "price_basis": "official_unadjusted_final_1500_close",
            "price": 10, "published_at": AT.replace(minute=0).isoformat(),
            "received_at": AT.replace(minute=0, second=1).isoformat(),
            "available_at": AT.replace(minute=0, second=2).isoformat()},
        "initial": {"record_id": "complete.initial", "source_at": AT.replace(minute=5).isoformat(),
                    "sequence": 1, "orders": [external()]},
        "events": [], "last_sequence": 1}


def local(identifier="intent.1", *, sequence=1, quantity=100, side="buy", limit=10.5, account="default"):
    accepted = AT - timedelta(seconds=1)
    proof = {"contract_version": CONTRACT, "mandatory": True, "fillable": False, "simulation_only": True,
             "order_type": MODE, "order_id": identifier, "acceptance_sequence": sequence, "account_id": account,
             "code": "600000", "side": side, "original_limit_price": limit, "original_quantity": quantity,
             "requested_at": accepted.isoformat(), "accepted_at": accepted.isoformat(),
             "validated_at": accepted.isoformat(), "terminal_validated_at": accepted.isoformat(),
             "session_end_at": AT.replace(minute=30).isoformat()}
    return SimpleNamespace(id=sequence, order_id=identifier, order_type=MODE, broker="paper",
        account_id=account, code="600000", side=side, price=limit, quantity=quantity,
        status="submitted", filled_quantity=0, trade_date=AT.date(), risk_json=json.dumps({
            "paper_after_hours_intent": proof}))


def plan(payload=None, orders=None, **kwargs):
    return allocation.propose_allocations(feed() if payload is None else payload,
        [local()] if orders is None else orders, now=kwargs.pop("now", NOW), scenario_account="default", **kwargs)


def rehash(proposal):
    proposal["proposal_id"] = allocation._hash({k: v for k, v in proposal.items() if k != "proposal_id"})


def test_production_catalog_no_order_level_fill_authority():
    capability = allocation.matching_capabilities()
    assert capability["order_level_provider_count"] == 0
    assert not capability["execution_authorized"] and not capability["ledger_fill_contract_enabled"]
    payload = feed()
    for source, profile in allocation._PROVIDERS.items():
        payload.update(source=source, source_version=profile.version, queue_verified=True,
                       capability="order_level", execution_authorized=True,
                       counterparty_quantity=10**12, external_queue_ahead_shares=0)
        result = plan(payload)
        assert result["reason"] == "provider_aggregate_only" and result["proposals"] == []
        assert result["fills"] == [] and not result["fillable"] and not result["execution_authorized"]
    payload["source"] = "client.invented.provider"
    assert plan(payload)["reason"] == "provider_not_audited"


def test_fixture_allocation_fixed_close_fifo_and_no_input_mutation(audited_fixture):
    payload = feed()
    before = deepcopy(payload)
    orders = [local("later", sequence=2), local("earlier", sequence=1)]
    result = plan(payload, orders)
    assert result["status"] == "proposal_only" and result["fills"] == []
    assert result["execution_authorized"] is result["fillable"] is False
    assert [p["order_id"] for p in result["proposals"]] == ["earlier", "later"]
    assert [p["fixed_price"] for p in result["proposals"]] == [10, 10]
    assert [p["resources"][0]["offset"] for p in result["proposals"]] == [0, 100]
    assert result == plan(payload, orders) and payload == before


@pytest.mark.parametrize("change", [
    lambda p: p.update(last_sequence=2),
    lambda p: p.update(exchange="SZSE"),
    lambda p: p.update(session_kind="continuous"),
    lambda p: p.update(rule_version="before.2026"),
    lambda p: p.update(security_state_1500="suspended"),
    lambda p: p.update(source_version="other"),
    lambda p: p.update(verification_method="queue_verified_boolean"),
    lambda p: p["initial"].update(source_at=AT.replace(minute=6).isoformat()),
    lambda p: p["initial"]["orders"][0].update(unit="hands"),
    lambda p: p["initial"]["orders"][0].update(quantity=True),
    lambda p: p["initial"]["orders"][0].update(limit_price=10.001),
    lambda p: p["official_close"].update(price_basis="forward_adjusted"),
    lambda p: p["official_close"].update(price=float("nan")),
    lambda p: p["official_close"].update(price="1e999999"),
    lambda p: p["official_close"].update(price="10.00000000000000000000000000001"),
    lambda p: p["official_close"].update(price="100000000000000000000000001.00"),
    lambda p: p["official_close"].update(code="600001"),
    lambda p: p["official_close"].update(available_at=(NOW + timedelta(seconds=1)).isoformat()),
])
def test_invalid_contract_blocks_all_proposals(audited_fixture, change):
    payload = feed()
    change(payload)
    result = plan(payload)
    assert result["status"] == "evidence_blocked" and result["proposals"] == []
    assert result["fills"] == []


@pytest.mark.parametrize("now", [AT.replace(minute=30), AT + timedelta(seconds=15),
                              AT - timedelta(seconds=1), AT + timedelta(days=1)])
def test_frame_and_session_clocks_do_not_extend_fixed_close_authority(audited_fixture, now):
    result = plan(now=now)
    assert result["proposals"] == [] and result["fills"] == []


def test_close_is_fixed_not_subject_to_ordinary_quote_ttl(audited_fixture):
    payload = feed()
    payload.update(source_quote_at=AT.replace(minute=29, second=58).isoformat(),
                   received_at=AT.replace(minute=29, second=58, microsecond=100000).isoformat(),
                   available_at=AT.replace(minute=29, second=58, microsecond=200000).isoformat())
    result = plan(payload, now=AT.replace(minute=29, second=59))
    assert result["status"] == "proposal_only" and result["proposals"][0]["fixed_price"] == 10


def test_smaller_order_never_jumps_full_fill_only_head(audited_fixture):
    payload = feed()
    payload["initial"]["orders"][0]["quantity"] = 100
    orders = [local("small", sequence=2), local("head", sequence=1, quantity=200)]
    result = plan(payload, orders)
    assert result["proposals"] == []
    assert [item["reason"] for item in result["waiting"]] == [
        "insufficient_unconsumed_order_level_counterparty", "earlier_local_head_not_fully_allocatable"]
    orders[1].status = "canceled"
    result = plan(payload, orders)
    assert result["proposals"][0]["order_id"] == "small"


def test_actual_remaining_predecessors_and_cancellation_not_volume_difference(audited_fixture):
    payload = feed()
    payload["initial"]["orders"] = [external("ahead", side="buy")]
    result = plan(payload)
    assert result["waiting"][0]["verified_external_ahead_shares"] == 300
    assert result["waiting"][0]["reason"] == "verified_external_predecessors_still_live"
    payload["events"] = [{"record_id": "cancel.1", "sequence": 2, "source_at": AT.replace(minute=6).isoformat(),
                          "kind": "cancel", "order_id": "ahead", "quantity": 300}]
    payload["last_sequence"] = 2
    result = plan(payload)
    assert result["waiting"][0]["verified_external_ahead_shares"] == 0
    assert result["proposals"] == []  # Cancellation is NOT a counterparty trade.


def test_lifecycle_trade_consumption_and_prior_slice_overlap(audited_fixture):
    first = plan()["proposals"][0]
    payload = feed()
    buy = external("real.buy", side="buy", quantity=50, sequence=2, accepted=AT.replace(minute=6))
    payload["events"] = [
        {"record_id": "add.2", "sequence": 2, "source_at": buy["accepted_at"], "kind": "add", "order": buy},
        {"record_id": "trade.3", "sequence": 3, "source_at": buy["accepted_at"], "kind": "trade",
         "buy_order_id": "real.buy", "sell_order_id": "offer", "quantity": 50, "price": 10},
    ]
    payload.update(last_sequence=3, frame_id="frame.2")
    result = plan(payload, [local("intent.2", sequence=2)], prior_proposals=[first])
    assert result["proposals"][0]["resources"][0]["offset"] == 100
    assert result["proposals"][0]["quantity"] == 100
    # Reusing the old proposal is not a second allocation or broker receipt.
    assert plan(orders=[local()], prior_proposals=[first])["proposals"] == []
    assert plan(prior_proposals=[first, first])["status"] == "evidence_blocked"


def test_prior_resource_bounds_and_cross_scenario_rejected_even_rehashed(audited_fixture):
    prior = plan()["proposals"][0]
    for changes in [lambda p: p.update(scenario_account="promotion"),
                    lambda p: p["resources"][0].update(offset=250),
                    lambda p: p["resources"][0].update(source_order_hash="0" * 64),
                    lambda p: p.update(execution_authorized=True),
                    lambda p: p.update(proposed_at=(AT - timedelta(days=1)).isoformat()),
                    lambda p: p.update(source_evidence_hash="invented")]:
        changed = deepcopy(prior)
        changes(changed)
        rehash(changed)
        assert plan(orders=[local("later", sequence=2)], prior_proposals=[changed])["status"] == "evidence_blocked"


def test_different_accounts_are_not_local_mutual_counterparties(audited_fixture):
    assert plan(orders=[local(account="promotion")])["status"] == "evidence_blocked"
    empty = feed()
    empty["initial"]["orders"] = []
    result = plan(empty, [local("buy"), local("sell", sequence=2, side="sell", limit=9.5)])
    assert result["proposals"] == [] and result["fills"] == []


def test_bad_late_lifecycle_cannot_keep_an_earlier_proposal(audited_fixture):
    payload = feed()
    payload["events"] = [{"record_id": "gap", "sequence": 3, "source_at": AT.isoformat(), "kind": "cancel",
                          "order_id": "offer", "quantity": 300}]
    payload["last_sequence"] = 3
    assert plan(payload)["status"] == "evidence_blocked"
    payload = feed()
    payload["initial"]["orders"].append(external("crossed", side="buy", sequence=2))
    payload["initial"]["sequence"] = payload["last_sequence"] = 2
    assert plan(payload)["status"] == "evidence_blocked"


def test_trade_cannot_skip_earlier_external_order(audited_fixture):
    payload = feed()
    payload["initial"]["orders"] = [external("offer.head", sequence=1), external("offer.later", sequence=2),
                                    external("real.buy", side="buy", quantity=100, sequence=3)]
    payload["initial"]["sequence"] = 3
    payload["events"] = [{"record_id": "trade.4", "sequence": 4, "source_at": AT.replace(minute=5).isoformat(),
        "kind": "trade", "buy_order_id": "real.buy", "sell_order_id": "offer.later", "quantity": 100, "price": 10}]
    payload["last_sequence"] = 4
    assert plan(payload)["status"] == "evidence_blocked"


def test_multiple_counterparties_conserve_native_share_slices(audited_fixture):
    payload = feed()
    payload["initial"]["orders"] = [external("a", quantity=70, sequence=1), external("b", quantity=130, sequence=2)]
    payload["initial"]["sequence"] = payload["last_sequence"] = 2
    result = plan(payload, [local("one", sequence=1), local("two", sequence=2)])
    assert [[(r["external_order_id"], r["offset"], r["shares"]) for r in p["resources"]]
            for p in result["proposals"]] == [[("a", 0, 70), ("b", 0, 30)], [("b", 30, 100)]]
    assert sum(p["quantity"] for p in result["proposals"]) == 200


def test_full_fill_prior_order_cannot_be_reissued_on_disjoint_slices(audited_fixture):
    first = plan()["proposals"][0]
    second = plan(orders=[local("other", sequence=2)], prior_proposals=[first])["proposals"][0]
    second["order_id"] = first["order_id"]
    rehash(second)
    assert plan(orders=[], prior_proposals=[first, second])["status"] == "evidence_blocked"


def test_source_clock_and_sequence_cannot_roll_back_after_prior_proposal(audited_fixture):
    first = plan()["proposals"][0]
    payload = feed()
    for key in ("source_quote_at", "received_at", "available_at"):
        payload[key] = (datetime.fromisoformat(payload[key]) - timedelta(milliseconds=500)).isoformat()
    assert plan(payload, [local("later", sequence=2)], prior_proposals=[first])["status"] == "evidence_blocked"
    forged = deepcopy(first)
    forged["terminal_sequence"] = 2
    rehash(forged)
    assert plan(orders=[local("later", sequence=2)], prior_proposals=[forged])["status"] == "evidence_blocked"


def traded_feed(quantity):
    payload = feed()
    buy = external("real.buy", side="buy", quantity=quantity, sequence=2, accepted=AT.replace(minute=6))
    payload["events"] = [
        {"record_id": "add.2", "sequence": 2, "source_at": buy["accepted_at"], "kind": "add", "order": buy},
        {"record_id": "trade.3", "sequence": 3, "source_at": buy["accepted_at"], "kind": "trade",
         "buy_order_id": "real.buy", "sell_order_id": "offer", "quantity": quantity, "price": 10},
    ]
    payload.update(last_sequence=3, frame_id="frame.2")
    return payload


def test_seen_lifecycle_cannot_be_rewritten_with_same_terminal_and_clocks(audited_fixture):
    first = plan(traded_feed(100))["proposals"][0]
    assert first["resources"][0]["offset"] == 100
    revision = traded_feed(50)
    revision["frame_id"] = "frame.3"
    result = plan(revision, [local("intent.2", sequence=2)], prior_proposals=[first])
    assert result["status"] == "evidence_blocked" and result["proposals"] == []
    # An old, still-fresh frame cannot resurrect those first 100 actually traded shares.
    assert plan(feed(), [local("intent.2", sequence=2)], prior_proposals=[first])["status"] == "evidence_blocked"


def test_seen_prefix_allows_valid_append_but_not_initial_revision(audited_fixture):
    original = traded_feed(50)
    first = plan(original)["proposals"][0]
    appended = deepcopy(original)
    appended["events"].append({"record_id": "add.4", "sequence": 4,
        "source_at": AT.replace(minute=7).isoformat(), "kind": "add",
        "order": external("offer.2", quantity=100, sequence=4, accepted=AT.replace(minute=7))})
    appended.update(last_sequence=4, frame_id="frame.3")
    result = plan(appended, [local("intent.2", sequence=2)], prior_proposals=[first])
    assert result["status"] == "proposal_only"
    assert result["proposals"][0]["resources"][0]["offset"] == 150
    revision = deepcopy(original)
    revision["initial"]["record_id"] = "rewritten.initial"
    revision["frame_id"] = "frame.3"
    assert plan(revision, [local("intent.2", sequence=2)], prior_proposals=[first])["status"] == "evidence_blocked"


def test_same_frame_identity_cannot_be_republished_with_new_content(audited_fixture):
    first = plan()["proposals"][0]
    revision = feed()
    revision["untrusted_extra_metadata"] = "changed"
    assert plan(revision, [local("intent.2", sequence=2)], prior_proposals=[first])["status"] == "evidence_blocked"


def test_future_local_validation_cannot_enter_a_proposal(audited_fixture):
    order = local()
    proof = json.loads(order.risk_json)
    for key in ("validated_at", "terminal_validated_at"):
        proof["paper_after_hours_intent"][key] = AT.replace(minute=20).isoformat()
    order.risk_json = json.dumps(proof)
    result = plan(orders=[order])
    assert result["status"] == "evidence_blocked" and result["proposals"] == []


def test_malformed_local_object_blocks_without_exception(audited_fixture):
    for malformed in (None, {}, SimpleNamespace(order_type=MODE)):
        assert plan(orders=[malformed])["status"] == "evidence_blocked"


def test_budgets_accept_exact_boundary_and_block_next_row_or_byte(audited_fixture, monkeypatch):
    payload = feed()
    monkeypatch.setattr(allocation, "MAX_BYTES", len(_json(payload).encode()))
    assert plan(payload)["status"] == "proposal_only"
    monkeypatch.setattr(allocation, "MAX_BYTES", len(_json(payload).encode()) - 1)
    assert plan(payload)["status"] == "evidence_blocked"
    monkeypatch.setattr(allocation, "MAX_BYTES", 2 * 1024 * 1024)

    monkeypatch.setattr(allocation, "MAX_ORDERS", 1)
    assert plan(payload)["status"] == "proposal_only"
    payload["initial"]["orders"].append(external("offer.2", sequence=2))
    payload["initial"]["sequence"] = payload["last_sequence"] = 2
    assert plan(payload)["status"] == "evidence_blocked"
    monkeypatch.setattr(allocation, "MAX_ORDERS", 2000)

    payload = traded_feed(50)
    monkeypatch.setattr(allocation, "MAX_EVENTS", 2)
    assert plan(payload)["status"] == "proposal_only"
    monkeypatch.setattr(allocation, "MAX_EVENTS", 1)
    assert plan(payload)["status"] == "evidence_blocked"
    monkeypatch.setattr(allocation, "MAX_EVENTS", 10000)

    monkeypatch.setattr(allocation, "MAX_LOCAL_ORDERS", 2)
    orders = [local("one", sequence=1), local("two", sequence=2)]
    assert len(plan(orders=orders)["proposals"]) == 2
    assert plan(orders=orders + [local("three", sequence=3)])["status"] == "evidence_blocked"
    priors = plan(orders=orders)["proposals"]
    assert plan(orders=[], prior_proposals=priors)["status"] == "waiting"
    monkeypatch.setattr(allocation, "MAX_LOCAL_ORDERS", 1)
    assert plan(orders=[], prior_proposals=priors)["status"] == "evidence_blocked"


def test_prior_resource_budget_does_not_silently_drop_consumption(audited_fixture, monkeypatch):
    payload = feed()
    payload["initial"]["orders"] = [external("a", quantity=70, sequence=1), external("b", quantity=130, sequence=2)]
    payload["initial"]["sequence"] = payload["last_sequence"] = 2
    prior = plan(payload)["proposals"][0]
    monkeypatch.setattr(allocation, "MAX_EVENTS", 2)
    assert plan(payload, orders=[], prior_proposals=[prior])["status"] == "waiting"
    monkeypatch.setattr(allocation, "MAX_EVENTS", 1)
    assert plan(payload, orders=[], prior_proposals=[prior])["status"] == "evidence_blocked"


@pytest.mark.parametrize("state,filled", [("partial", 50), ("filled", 100), ("submitted", 1), ("canceled", 50)])
def test_full_fill_only_kernel_rejects_partial_or_economic_local_state(audited_fixture, state, filled):
    order = local()
    order.status, order.filled_quantity = state, filled
    assert plan(orders=[order])["status"] == "evidence_blocked"


def test_trade_after_cancel_and_duplicate_acceptance_sequence_fail_closed(audited_fixture):
    payload = feed()
    payload["initial"]["orders"].append(external("duplicate", sequence=1))
    assert plan(payload)["status"] == "evidence_blocked"
    payload = traded_feed(100)
    payload["events"].insert(0, {"record_id": "cancel.2", "sequence": 2,
        "source_at": AT.replace(minute=6).isoformat(), "kind": "cancel", "order_id": "offer", "quantity": 300})
    for sequence, event in enumerate(payload["events"], 2):
        event["sequence"] = sequence
        if event["kind"] == "add":
            event["order"]["sequence"] = sequence
    payload["last_sequence"] = 4
    assert plan(payload)["status"] == "evidence_blocked"


def test_local_sequence_and_proof_budget_are_fail_closed(audited_fixture, monkeypatch):
    first, second = local("one"), local("two")
    assert plan(orders=[first, second])["status"] == "evidence_blocked"
    proof_bytes = len(first.risk_json.encode())
    monkeypatch.setattr(allocation, "MAX_LOCAL_PROOF_BYTES", proof_bytes)
    assert plan(orders=[first])["status"] == "proposal_only"
    monkeypatch.setattr(allocation, "MAX_LOCAL_PROOF_BYTES", proof_bytes - 1)
    assert plan(orders=[first])["status"] == "evidence_blocked"
    monkeypatch.setattr(allocation, "MAX_LOCAL_PROOF_BYTES", proof_bytes)
    assert plan(orders=[first, local("two", sequence=2)])["status"] == "evidence_blocked"


def test_interval_subtraction_conserves_capacity_without_duplicate_offsets():
    import random
    randomizer = random.Random(20261002)
    for _ in range(1000):
        left, right = sorted(randomizer.sample(range(31), 2))
        points = sorted(randomizer.sample(range(31), 10))
        consumed = list(zip(points[::2], points[1::2]))
        remaining = allocation._subtract((left, right), consumed)
        actual = [x for a, b in remaining for x in range(a, b)]
        expected = set(range(left, right)) - {x for a, b in consumed for x in range(a, b)}
        assert set(actual) == expected and len(actual) == len(set(actual))
        assert remaining == sorted(remaining)


def test_canceled_counterparty_has_no_live_capacity(audited_fixture):
    payload = feed()
    payload["events"] = [{"record_id": "cancel.2", "sequence": 2, "source_at": AT.replace(minute=6).isoformat(),
                          "kind": "cancel", "order_id": "offer", "quantity": 300}]
    payload["last_sequence"] = 2
    assert plan(payload)["proposals"] == []
    payload["events"][0]["quantity"] = 100
    assert plan(payload)["status"] == "evidence_blocked"
