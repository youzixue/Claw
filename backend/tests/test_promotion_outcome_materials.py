"""Only explicit isolated protocol fixtures; none is an approved vendor parser."""
import asyncio
from dataclasses import replace
from datetime import date, datetime, timedelta
import os
import threading

import pytest

from app.promotion import outcome_materials as outcome
from app.promotion.modeling.daily_materials import MaterialArchive, ReviewedSourcePolicy, encode, decode, digest

# Isolated future-clock scenario; physical files exist before simulated publication.
DAYS = ((datetime.now() + timedelta(days=2)).date(),
        (datetime.now() + timedelta(days=3)).date())
PREDICTION_AT = datetime.combine(DAYS[0], datetime.min.time()) + timedelta(hours=20, minutes=30)
CODES = ("600001", "600002")


def parser(raw):
    value = decode(raw)
    if value.pop("isolated_protocol", None) != "outcome_fixture_only_v1":
        raise ValueError("not_explicit_fixture_protocol")
    return value


@pytest.fixture
def archive(tmp_path, monkeypatch):
    policy = ReviewedSourcePolicy("isolated_outcome_fixture_v1", {"ths:fixture": parser})
    result = MaterialArchive(tmp_path, policy=policy)
    result.fixture_clock = [datetime.combine(DAYS[1], datetime.min.time()) + timedelta(hours=21)]
    started = datetime.now()
    clock = lambda: result.fixture_clock[0] + (datetime.now() - started)
    result._clock = clock
    monkeypatch.setattr(outcome, "_now", clock)
    return result


def material(archive, day, kind, mutate=None):
    value = {"isolated_protocol": "outcome_fixture_only_v1", "kind": kind,
             "source": "ths", "source_version": "fixture", "trade_date": str(day),
             "complete": True, "finality_verified": True, "universe_verified": True,
             "expected_from_protocol": True, "universe_codes": list(CODES),
             "expected_codes": list(CODES) if kind == "close" else ["600001"],
             "universe_frozen_at": str(day) + "T09:00:00",
             "source_quote_at": str(day) + "T15:01:00",
             "source_published_at": str(day) + "T15:02:00",
             "finality_evidence": {"kind": "isolated_fixture", "reference": "fixture:final-message"},
             "price_basis": "CNY_per_share", "adjustment_basis": "forward_adjusted",
             "adjustment_version": "fixture-same-basis-v1", "volume_unit": "shares", "amount_unit": "CNY",
             "rows": [{"code": c, "status": "ok", "close": 10.0, "prev_close": 10.0,
                       "volume": 1000.0} for c in (CODES if kind == "close" else ("600001",))]}
    if mutate:
        mutate(value)
    return archive.archive(encode(value), {"source": "ths", "source_version": "fixture",
                                          "provenance": "forward_response"})


def publish(archive, day, mutate_close=None, mutate_pool=None):
    if day not in getattr(archive, "fixture_published_days", set()):
        archive.fixture_clock[0] = datetime.combine(day, datetime.min.time()) + timedelta(hours=20)
    archive.fixture_published_days = getattr(archive, "fixture_published_days", set()) | {day}
    close = material(archive, day, "close", mutate_close)
    pool = material(archive, day, "pool", mutate_pool)
    receipt = outcome.append_outcome_evidence(close_ref=close, pool_ref=pool,
                                              archive_root=archive.root, policy=archive.policy)
    archive.fixture_clock[0] = datetime.combine(DAYS[1], datetime.min.time()) + timedelta(hours=21)
    return close, pool, receipt


def index(archive, cutoff=None, policy=None):
    return asyncio.run(outcome.prepare_outcome_index(known_cutoff=cutoff or outcome._now(),
                                                     archive_root=archive.root,
                                                     policy=policy or archive.policy))


def gate(idx, codes=CODES, evaluation=None):
    return outcome.outcome_pair_gate(idx, codes=codes, prediction_day=DAYS[0], prediction_at=PREDICTION_AT,
                                      outcome_day=DAYS[1],
                                      evaluation_as_of=evaluation or idx.known_cutoff)


def test_valid_pair_batch_worker_and_shared_namespace(archive, monkeypatch):
    for day in DAYS:
        publish(archive, day)
    main = threading.get_ident()
    original = outcome._prepare
    ids = []
    def checked(**kwargs):
        ids.append(threading.get_ident())
        return original(**kwargs)
    monkeypatch.setattr(outcome, "_prepare", checked)
    idx = index(archive)
    assert ids and ids[0] != main
    result = gate(idx)
    assert result["passed"], result
    assert [r["outcome_limit_up"] for r in result["per_code"]] == [True, False]
    assert result["per_code"][0]["before_close"] == 10.0
    assert not (archive.root / "ready").exists()
    assert len(list((archive.root / "outcome_receipts" / "ready").glob("*.blob"))) == 2
    monkeypatch.setattr(outcome, "_observe", lambda *a: pytest.fail("gate performed IO"))
    assert gate(idx)["passed"]


def test_default_policy_and_missing_material_fail_closed(archive):
    for day in DAYS:
        publish(archive, day)
    idx = asyncio.run(outcome.prepare_outcome_index(known_cutoff=outcome._now(), archive_root=archive.root))
    assert not gate(idx)["passed"]
    assert all(r["outcome_limit_up"] is None for r in gate(idx)["per_code"])


@pytest.mark.parametrize("mutate", [
    lambda p: p.update(complete=False),
    lambda p: p.update(finality_verified=False),
    lambda p: p.update(expected_from_protocol=False),
    lambda p: p.update(source_published_at=None),
    lambda p: p.update(source_published_at="2099-01-01T15:00:00"),
    lambda p: p.update(universe_frozen_at=str(DAYS[1]) + "T15:00:00"),
    lambda p: p.update(finality_evidence={}),
    lambda p: p.update(adjustment_basis="raw"),
    lambda p: p.update(volume_unit="hands"),
    lambda p: p["rows"].pop(),
    lambda p: p["rows"].append(dict(p["rows"][0])),
    lambda p: p["rows"].append(None),
    lambda p: p["rows"][1].update(status="unknown"),
    lambda p: p["rows"][1].update(volume=0),
    lambda p: p["rows"][1].update(prev_close=9.0),
    lambda p: p.update(adjustment_version="different"),
])
def test_one_bad_non_candidate_blocks_whole_batch(archive, mutate):
    publish(archive, DAYS[0])
    publish(archive, DAYS[1], mutate_close=mutate)
    result = gate(index(archive), codes=("600001",))
    assert not result["passed"]
    assert result["per_code"][0]["outcome_limit_up"] is None


def test_empty_pool_contract_not_relaxed(archive):
    publish(archive, DAYS[0])
    publish(archive, DAYS[1], mutate_pool=lambda p: p.update(expected_codes=[], rows=[]))
    idx = index(archive)
    assert not gate(idx)["passed"]
    assert "zero_pool_contract_not_supported" in str(decode(idx.payload))


def test_stable_semantics_and_full_handle_integrity(archive):
    for day in DAYS:
        publish(archive, day)
    a, b = index(archive), index(archive)
    assert a.payload_hash != b.payload_hash
    assert a.evidence_hash == b.evidence_hash
    assert gate(a)["evidence_hash"] == gate(b)["evidence_hash"]
    body = decode(a.payload)
    body["known_cutoff"] = b.known_cutoff.isoformat()
    bad = replace(a, payload=encode(body), known_cutoff=b.known_cutoff)
    assert not gate(bad)["passed"]


def test_new_revision_changes_hash_no_fallback_and_locked_handle_stable(archive):
    for day in DAYS:
        publish(archive, day)
    old = index(archive)
    publish(archive, DAYS[1], mutate_close=lambda p: p["rows"].pop())
    latest = index(archive)
    assert gate(old)["passed"]
    assert not gate(latest)["passed"]
    assert old.evidence_hash != latest.evidence_hash
    assert index(archive, cutoff=old.known_cutoff).evidence_hash == old.evidence_hash


def test_valid_new_revision_and_same_known_conflict(archive, monkeypatch):
    for day in DAYS:
        publish(archive, day)
    old = index(archive)
    _, _, ref = publish(archive, DAYS[1], mutate_pool=lambda p: p.update(
        expected_codes=["600002"], rows=[{"code": "600002", "status": "ok"}]))
    new = index(archive)
    assert gate(new)["passed"]
    assert gate(new)["per_code"][0]["outcome_limit_up"] is False
    assert gate(old)["evidence_hash"] != gate(new)["evidence_hash"]
    # Explicit corrupt-handle fixture tests a same-clock competing revision.
    body = decode(new.payload)
    records = [r for r in body["records"] if r["trade_date"] == str(DAYS[1])]
    for r in records:
        r["known_at"] = max(x["known_at"] for x in records)
        r["declared_known_at"] = r["known_at"]
    raw = encode(body)
    bad = outcome.OutcomeIndex(raw, new.known_cutoff, outcome._semantic(body), digest(raw))
    assert "same_known_receipt_conflict" in gate(bad)["reasons"]


def test_corrupt_raw_observed_bytes_change_rejected_hash(archive):
    publish(archive, DAYS[0])
    close, _, _ = publish(archive, DAYS[1])
    envelope = decode(archive.read_bytes(close))
    path = archive.root / envelope["raw_ref"]["path"]
    path.write_bytes(b"x")
    a = index(archive)
    path.write_bytes(b"y")
    b = index(archive)
    assert not gate(a)["passed"] and not gate(b)["passed"]
    assert a.evidence_hash != b.evidence_hash
    assert "observed_sha256" in str(decode(a.payload))


def test_future_cutoff_and_future_corrupt_receipt(archive):
    for day in DAYS:
        publish(archive, day)
    old = index(archive)
    with pytest.raises(ValueError, match="future"):
        index(archive, cutoff=outcome._now() + timedelta(days=1))
    directory = archive.root / "outcome_receipts" / "ready"
    path = directory / ("a" * 64 + ".blob")
    path.write_bytes(b"bad")
    future = (outcome._now() + timedelta(seconds=5)).timestamp()
    os.utime(path, (future, future))
    assert index(archive, cutoff=old.known_cutoff).evidence_hash == old.evidence_hash


@pytest.mark.parametrize("codes", [(), ("600001", "600001"), ("６００００１",), ("999999",)])
def test_invalid_or_missing_candidate_unknown(archive, codes):
    for day in DAYS:
        publish(archive, day)
    assert not gate(index(archive), codes=codes)["passed"]


def test_missing_file_no_mutable_fallback(archive):
    publish(archive, DAYS[0])
    close, _, _ = publish(archive, DAYS[1])
    (archive.root / close["path"]).unlink()
    assert not gate(index(archive))["passed"]


def test_shared_raw_read_once_even_with_republished_receipt(archive, monkeypatch):
    publish(archive, DAYS[0])
    close, pool, _ = publish(archive, DAYS[1])
    outcome.append_outcome_evidence(close_ref=close, pool_ref=pool,
                                    archive_root=archive.root, policy=archive.policy)
    original = outcome.os.open
    counts = {}
    def counted(path, *args, **kwargs):
        key = str(path)
        counts[key] = counts.get(key, 0) + 1
        return original(path, *args, **kwargs)
    monkeypatch.setattr(outcome.os, "open", counted)
    assert gate(index(archive))["passed"]
    assert counts and max(counts.values()) == 1


def test_parent_mutation_cannot_change_locked_gate(archive):
    for day in DAYS:
        publish(archive, day)
    locked = index(archive)
    previous = gate(locked)
    body = decode(locked.payload)
    body["records"][0]["close"]["parsed"]["rows"][0]["close"] = 999
    assert gate(locked) == previous


def test_invalid_handle_and_late_query_are_unknown(archive):
    for day in DAYS:
        publish(archive, day)
    locked = index(archive)
    for raw in (b"{}", b"[]", b"not JSON"):
        assert not gate(replace(locked, payload=raw))["passed"]
    assert not gate(locked, evaluation=locked.known_cutoff + timedelta(microseconds=1))["passed"]


def test_append_does_not_accept_backfilled_known_at(archive):
    close = material(archive, DAYS[0], "close")
    pool = material(archive, DAYS[0], "pool")
    with pytest.raises(TypeError):
        outcome.append_outcome_evidence(close_ref=close, pool_ref=pool,
                                        known_at=datetime(2020, 1, 1), archive_root=archive.root)


def test_symlink_material_rejected(archive, tmp_path):
    publish(archive, DAYS[0])
    close, _, _ = publish(archive, DAYS[1])
    path = archive.root / close["path"]
    raw = path.read_bytes()
    path.unlink()
    outside = tmp_path / "outside"
    outside.write_bytes(raw)
    path.symlink_to(outside)
    assert not gate(index(archive))["passed"]


def test_prediction_material_must_be_known_by_original_run(archive):
    for day in DAYS:
        publish(archive, day)
    locked = index(archive)
    result = outcome.outcome_pair_gate(locked, codes=CODES, prediction_day=DAYS[0],
        prediction_at=PREDICTION_AT.replace(hour=19), outcome_day=DAYS[1],
        evaluation_as_of=locked.known_cutoff)
    assert not result["passed"]
    assert all(r["prediction_limit_up"] is None for r in result["per_code"])


def test_late_prediction_revision_cannot_replace_frozen_prediction(archive):
    for day in DAYS:
        publish(archive, day)
    original = index(archive)
    publish(archive, DAYS[0], mutate_pool=lambda p: p.update(
        expected_codes=["600002"], rows=[{"code": "600002", "status": "ok"}]))
    latest = index(archive)
    assert gate(latest)["passed"]
    assert gate(latest)["per_code"][0]["prediction_limit_up"] is True
    assert gate(latest)["evidence_hash"] != gate(original)["evidence_hash"]
    row = gate(latest)["per_code"][0]
    assert row["price_basis"] == "CNY_per_share"
    assert row["adjustment_basis"] == "forward_adjusted"
    assert row["volume_unit"] == "shares"


def test_prediction_clock_date_and_hash(archive):
    for day in DAYS:
        publish(archive, day)
    locked = index(archive)
    def at(clock):
        return outcome.outcome_pair_gate(locked, codes=CODES, prediction_day=DAYS[0],
            prediction_at=clock, outcome_day=DAYS[1], evaluation_as_of=locked.known_cutoff)
    assert not at(PREDICTION_AT + timedelta(days=1))["passed"]
    later = at(PREDICTION_AT + timedelta(microseconds=1))
    assert later["passed"] and later["evidence_hash"] != gate(locked)["evidence_hash"]


@pytest.mark.parametrize("handle", [None, {}, object(), type("Fake", (), {"payload": b"{}"})()])
def test_non_prepared_handles_never_escape(handle):
    result = outcome.outcome_pair_gate(handle, codes=CODES, prediction_day=DAYS[0],
        prediction_at=PREDICTION_AT, outcome_day=DAYS[1], evaluation_as_of=datetime.now())
    assert not result["passed"]
    assert all(r["status"] == "unknown" for r in result["per_code"])


@pytest.mark.parametrize("mutate", [
    lambda b: b.update(policy_id=""),
    lambda b: b.update(records=[None]),
    lambda b: b["records"][0].pop("receipt_available_at"),
    lambda b: b["records"][0].update(known_at="bad"),
    lambda b: b["records"][0]["close"].pop("available_at"),
    lambda b: b["records"][0]["close"]["ref"].update(path="../unsafe"),
    lambda b: b["records"][0]["close"]["parsed"].update(finality_verified=False),
    lambda b: b["records"][0]["close"]["parsed"].pop("source_published_at"),
    lambda b: b["records"][0]["close"]["parsed"].update(finality_evidence={}),
    lambda b: b["records"][0]["close"]["envelope"].update(provenance="observed_now"),
])
def test_rehashed_malformed_prepared_structure_is_not_a_proof(archive, mutate):
    for day in DAYS:
        publish(archive, day)
    locked = index(archive)
    body = decode(locked.payload)
    mutate(body)
    raw = encode(body)
    forged = outcome.OutcomeIndex(raw, locked.known_cutoff, outcome._semantic(body), digest(raw))
    result = gate(forged)
    assert not result["passed"]
    assert all(r["outcome_limit_up"] is None for r in result["per_code"])


def test_verified_leaf_contract(archive):
    for day in DAYS:
        publish(archive, day)
    result = gate(index(archive))
    assert result["passed"]
    for row in result["per_code"]:
        assert row["status"] == "verified" and row["reasons"] == []
        assert type(row["prediction_limit_up"]) is bool
        assert type(row["outcome_limit_up"]) is bool


def test_unknown_repeated_read_semantics_stable(archive):
    for day in DAYS:
        publish(archive, day, mutate_pool=lambda p: p.update(finality_verified=False))
    a, b = index(archive), index(archive)
    assert not gate(a)["passed"]
    assert gate(a)["evidence_hash"] == gate(b)["evidence_hash"]
