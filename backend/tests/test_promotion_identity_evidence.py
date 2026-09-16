"""Isolated protocol/clock fixtures only; no real source is authorized here."""
import asyncio
from datetime import date, datetime, time, timedelta
import threading

import pytest

from app.promotion import identity_evidence as identity
from app.promotion.modeling.daily_materials import MaterialArchive, ReviewedSourcePolicy, decode, encode, digest


NORMAL = {"risk_warning": "normal", "listing_state": "listed", "delisting_risk": "clear",
          "suspension_state": "trading", "board": "main"}


def fixture_index(*, known_cutoff, codes=("600001",), unknown_code=None):
    """Synthetic prepared view for ledger unit tests, not an archive/parser."""
    records = []
    for i, code in enumerate(codes):
        if code == unknown_code:
            continue
        records.append({"event_id": "fixture-" + code, "code": code, "instrument_id": "fixture-security:" + code,
            "effective_from": "2020-01-01T00:00:00", "effective_to": "2030-01-01T00:00:00",
            "effective_verified": True, "state": dict(NORMAL), "supersedes": [],
            "source_published_at": None, "known_at": "2020-01-01T00:00:00",
            "received_at": "2020-01-01T00:00:00", "observed_at": "2020-01-01T00:00:00",
            "archive_created_at": "2020-01-01T00:00:00",
            "receipt_hash": digest(f"fixture-receipt-{i}".encode()),
            "record_hash": digest(f"fixture-record-{i}".encode()), "raw_hash": digest(f"fixture-raw-{i}".encode())})
    payload = encode({"profile": identity.PROFILE, "policy_id": "synthetic_prepared_index_only",
                      "known_cutoff": known_cutoff.isoformat(), "records": records,
                      "errors": [], "receipt_refs": [r["receipt_hash"] for r in records]})
    return identity.IdentityIndex(payload, known_cutoff, identity._semantic_index_hash(decode(payload)), digest(payload))


def parser(raw):
    parsed = decode(raw)
    if parsed.pop("fixture_protocol", None) != "reviewed_identity_test_only":
        raise ValueError("isolated fixture protocol missing")
    return parsed


@pytest.fixture
def env(tmp_path, monkeypatch):
    # Starts at real physical now; advancing this clock is exclusively a fixture.
    box = [datetime.now()]
    monkeypatch.setattr(identity, "_now", lambda: box[0])
    policy = ReviewedSourcePolicy("identity_fixture_protocol_v1", {"fixture:1": parser})
    return tmp_path, box, policy


def event(env, event_id="e1", code="600001", *, state=None, start=None, end=None, supersedes=()):
    _, box, _ = env
    day = box[0].date()
    return dict(event_id=event_id, code=code, instrument_id="fixture-security:" + code, effective_from=(start or datetime.combine(day - timedelta(days=5), time())).isoformat(),
                effective_to=(end or datetime.combine(day + timedelta(days=10), time())).isoformat(),
                effective_verified=True, state=dict(state or NORMAL),
                supersedes=list(supersedes), source_published_at=None)


def append(env, *events):
    root, box, policy = env
    box[0] = max(box[0], datetime.now()) + timedelta(seconds=1)
    raw = encode(dict(fixture_protocol="reviewed_identity_test_only", source="fixture", source_version="1",
                      identity_verified=True, events=list(events)))
    return identity.append_identity_evidence(raw, source="fixture", source_version="1",
                                             archive_root=root, policy=policy)


async def prepare(env):
    root, box, policy = env
    box[0] += timedelta(seconds=1)
    return await identity.prepare_identity_index(known_cutoff=box[0], archive_root=root, policy=policy)


def query(index, env, *, prediction=None, outcome=None, codes=("600001",)):
    _, box, _ = env
    outcome = outcome or box[0].date()
    prediction = prediction or datetime.combine(outcome - timedelta(days=1), time(20))
    return identity.identity_pair_gate(index, codes=codes, prediction_at=prediction,
                                      outcome_day=outcome, evaluation_as_of=box[0])


def resolve(index, code, at, known):
    state, refs, errors = identity._resolve(decode(index.payload)["records"], code, at, at + timedelta(microseconds=1), known)
    # These older tests compare state attributes; instrument identity has its own
    # pair-boundary tests below and must not be synthesized in production.
    return ({k: v for k, v in state.items() if k != "instrument_id"} if state else None), refs, errors


@pytest.mark.asyncio
async def test_real_append_and_threaded_prepare_full_pair(env, monkeypatch):
    append(env, event(env))
    prediction = env[1][0] + timedelta(minutes=1)
    env[1][0] = datetime.combine(prediction.date() + timedelta(days=1), time(16))
    caller = threading.get_ident()
    observed = []
    original = identity._read_observed
    def read(*args):
        observed.append(threading.get_ident())
        return original(*args)
    monkeypatch.setattr(identity, "_read_observed", read)
    index = await prepare(env)
    result = query(index, env, prediction=prediction)
    assert result["passed"], result
    assert observed and all(t != caller for t in observed)
    assert result["per_code"][0]["prediction_refs"]
    assert result["profile"] == identity.PROFILE


@pytest.mark.asyncio
async def test_default_policy_empty_and_absence_unknown(env):
    root, box, policy = env
    append(env, event(env))
    box[0] += timedelta(days=1)
    index = await identity.prepare_identity_index(known_cutoff=box[0], archive_root=root)
    assert decode(index.payload)["errors"]
    assert not query(index, env)["passed"]
    empty = await identity.prepare_identity_index(known_cutoff=box[0], archive_root=root / "missing")
    assert not query(empty, env)["passed"]
    assert not (root / "missing").exists()


@pytest.mark.asyncio
async def test_late_known_early_effective_never_backfills_prediction(env):
    early = env[1][0] - timedelta(minutes=1)
    append(env, event(env))
    env[1][0] += timedelta(days=1)
    index = await prepare(env)
    assert resolve(index, "600001", early, early)[2] == ["identity_interval_gap"]
    assert resolve(index, "600001", early, env[1][0])[0] == NORMAL


@pytest.mark.asyncio
async def test_early_known_later_effective_does_not_apply_early(env):
    boundary = env[1][0] + timedelta(days=1)
    append(env, event(env, end=boundary),
           event(env, "future-st", state={**NORMAL, "risk_warning": "ST"}, start=boundary))
    index = await prepare(env)
    assert resolve(index, "600001", boundary - timedelta(seconds=1), env[1][0])[0] == NORMAL
    assert resolve(index, "600001", boundary, env[1][0])[0]["risk_warning"] == "ST"


@pytest.mark.asyncio
async def test_correction_known_axis_and_deleted_superseded_no_fallback(env):
    at = env[1][0]
    first = append(env, event(env, state={**NORMAL, "risk_warning": "*ST"}))
    before = await prepare(env)
    append(env, event(env, "corrected", supersedes=("e1",)))
    after = await prepare(env)
    assert resolve(after, "600001", at, before.known_cutoff)[0]["risk_warning"] == "*ST"
    assert resolve(after, "600001", at, after.known_cutoff)[0] == NORMAL
    assert before.evidence_hash != after.evidence_hash
    root = env[0]
    (root / first["receipt_ref"]["path"]).unlink()
    missing = await prepare(env)
    assert "identity_superseded_missing_or_clock_conflict" in resolve(missing, "600001", at, missing.known_cutoff)[2]


@pytest.mark.asyncio
async def test_same_known_clock_overlap_conflicts(env):
    append(env, event(env), event(env, "conflict", state={**NORMAL, "risk_warning": "ST"}))
    index = await prepare(env)
    assert resolve(index, "600001", env[1][0], env[1][0])[2] == ["identity_interval_conflict"]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["morning_change", "lunch_change", "minute_gap", "closing_instant"])
async def test_entire_outcome_sessions_required(env, change):
    prediction = env[1][0] + timedelta(minutes=5)
    outcome = prediction.date() + timedelta(days=1)
    switch = datetime.combine(outcome, time(10) if change in {"morning_change", "minute_gap"} else time(12))
    if change == "closing_instant":
        append(env, event(env, end=datetime.combine(outcome, time(15))))
    else:
        append(env, event(env, end=switch),
               event(env, "next", start=switch + (timedelta(minutes=1) if change == "minute_gap" else timedelta()),
                     state=NORMAL if change == "minute_gap" else {**NORMAL, "suspension_state": "suspended"}))
    env[1][0] = datetime.combine(outcome, time(16))
    result = query(await prepare(env), env, prediction=prediction, outcome=outcome)
    assert not result["passed"]


@pytest.mark.asyncio
@pytest.mark.parametrize("state", [
    {**NORMAL, "risk_warning": "*ST"}, {**NORMAL, "delisting_risk": "warned"},
    {**NORMAL, "listing_state": "delisting_period"},
    {**NORMAL, "listing_state": "delisted"}, {**NORMAL, "suspension_state": "suspended"},
    {**NORMAL, "risk_warning": "unknown"}])
async def test_identity_types_not_collapsed_into_normal(env, state):
    append(env, event(env, state=state))
    prediction = env[1][0] + timedelta(minutes=1)
    env[1][0] = datetime.combine(prediction.date() + timedelta(days=1), time(16))
    result = query(await prepare(env), env, prediction=prediction)
    assert not result["passed"]


@pytest.mark.asyncio
async def test_raw_damage_and_future_cutoff_fail_closed(env):
    root, box, policy = env
    first = append(env, event(env))
    archive = MaterialArchive(root)
    receipt = decode(archive.read_bytes(first["receipt_ref"]))
    record = decode(archive.read_bytes(receipt["record_ref"]))
    path = root / record["raw_ref"]["path"]
    raw = path.read_bytes()
    path.write_bytes(b"x" + raw[1:])
    index = await prepare(env)
    assert decode(index.payload)["errors"]
    with pytest.raises(ValueError, match="cutoff"):
        await identity.prepare_identity_index(known_cutoff=box[0] + timedelta(microseconds=1),
                                              archive_root=root, policy=policy)


@pytest.mark.asyncio
async def test_mutable_current_status_unknown_and_no_known_at_argument(env):
    root, box, _ = env
    result = identity.append_identity_evidence(encode({"is_st": False}), source="StockTag", source_version="current",
                                              archive_root=root)
    assert result["status"] == "unknown"
    with pytest.raises(TypeError):
        identity.append_identity_evidence(b"{}", source="StockTag", source_version="current", known_at="2020")
    box[0] += timedelta(seconds=1)
    index = await identity.prepare_identity_index(known_cutoff=box[0], archive_root=root)
    assert decode(index.payload)["errors"]


@pytest.mark.asyncio
async def test_bad_event_structure_archived_unknown(env):
    result = append(env, {**event(env), "code": "６００００１"})
    assert result["status"] == "unknown"
    assert decode((await prepare(env)).payload)["errors"]


def test_missing_one_code_blocks_whole_batch_and_changes_hash():
    cutoff = datetime(2026, 9, 8, 20)
    index = fixture_index(known_cutoff=cutoff, codes=("600001", "600002"), unknown_code="600002")
    gate = identity.identity_pair_gate(index, codes=("600001", "600002"),
        prediction_at=datetime(2026, 9, 7, 20), outcome_day=date(2026, 9, 8), evaluation_as_of=cutoff)
    assert not gate["passed"] and len(gate["per_code"]) == 2
    known = fixture_index(known_cutoff=cutoff, codes=("600001", "600002"))
    good = identity.identity_pair_gate(known, codes=("600001", "600002"),
        prediction_at=datetime(2026, 9, 7, 20), outcome_day=date(2026, 9, 8), evaluation_as_of=cutoff)
    assert good["passed"] and good["evidence_hash"] != gate["evidence_hash"]


@pytest.mark.asyncio
async def test_midday_roundtrip_identity_change_is_not_hidden(env):
    prediction = env[1][0] + timedelta(minutes=5)
    outcome = prediction.date() + timedelta(days=1)
    noon = datetime.combine(outcome, time(12))
    resume = noon + timedelta(minutes=20)
    append(env, event(env, end=noon),
           event(env, "midday-suspended", start=noon, end=resume,
                 state={**NORMAL, "suspension_state": "suspended"}),
           event(env, "resumed", start=resume))
    env[1][0] = datetime.combine(outcome, time(16))
    result = query(await prepare(env), env, prediction=prediction, outcome=outcome)
    assert not result["passed"] and "identity_intraday_change" in result["reasons"]


@pytest.mark.asyncio
async def test_reingesting_old_event_does_not_move_first_known_after_correction(env):
    original = event(env, state={**NORMAL, "risk_warning": "ST"})
    append(env, original)
    old_known = (await prepare(env)).known_cutoff
    append(env, event(env, "new-normal", supersedes=("e1",)))
    append(env, original)
    index = await prepare(env)
    assert resolve(index, "600001", old_known, index.known_cutoff)[0] == NORMAL


@pytest.mark.asyncio
async def test_first_known_cannot_be_backdated_with_forged_record_clock(env):
    root, box, policy = env
    first = append(env, event(env))
    archive = MaterialArchive(root)
    receipt = decode(archive.read_bytes(first["receipt_ref"]))
    record = decode(archive.read_bytes(receipt["record_ref"]))
    old = datetime.now() - timedelta(days=30)
    for key in ("received_at", "observed_at", "archive_created_at"):
        record[key] = old.isoformat()
    ref = archive.put(encode(record))
    archive.put(encode({"schema": identity.SCHEMA, "record_ref": ref, "known_at": old.isoformat()}), area="ready")
    past = await identity.prepare_identity_index(known_cutoff=old + timedelta(days=1), archive_root=root, policy=policy)
    assert decode(past.payload)["records"] == []


def test_locked_index_hash_and_future_query_must_fail():
    from dataclasses import replace
    cutoff = datetime(2026, 9, 8, 20)
    index = fixture_index(known_cutoff=cutoff)
    broken = replace(index, evidence_hash="0" * 64)
    bad = identity.identity_pair_gate(broken, codes=("600001",), prediction_at=datetime(2026, 9, 7, 20),
                                     outcome_day=date(2026, 9, 8), evaluation_as_of=cutoff)
    assert "identity_index_hash_mismatch" in bad["reasons"]
    future = identity.identity_pair_gate(index, codes=("600001",), prediction_at=datetime(2026, 9, 7, 20),
        outcome_day=date(2026, 9, 8), evaluation_as_of=cutoff + timedelta(microseconds=1))
    assert "identity_query_clock_invalid" in future["reasons"]


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", [True, False])
async def test_security_identity_missing_or_changed_never_pairs_same_code(env, missing):
    prediction = env[1][0] + timedelta(minutes=5)
    boundary = datetime.combine(prediction.date() + timedelta(days=1), time())
    before = event(env, end=boundary)
    after = event(env, "security-replaced", start=boundary)
    if missing:
        after.pop("instrument_id")
    else:
        after["instrument_id"] = "fixture-distinct-security-not-same-issuer"
    append(env, before, after)
    env[1][0] = datetime.combine(boundary.date(), time(16))
    result = query(await prepare(env), env, prediction=prediction)
    assert not result["passed"]
    if not missing:
        assert "identity_instrument_changed_or_missing" in result["reasons"]


@pytest.mark.parametrize("bad", ["payload_type", "body_list", "records_none", "row_none", "interval",
    "missing_publication", "future_publication", "missing_known", "body_cutoff", "errors", "refs"])
def test_malformed_index_structure_is_rejected_without_crashing(bad):
    from dataclasses import replace
    cutoff = datetime(2026, 9, 8, 20)
    index = fixture_index(known_cutoff=cutoff)
    body = decode(index.payload)
    if bad == "payload_type":
        index = replace(index, payload=None)
    else:
        if bad == "body_list":
            body = []
        elif bad == "records_none":
            body["records"] = None
        elif bad == "row_none":
            body["records"][0] = None
        elif bad == "interval":
            body["records"][0]["effective_to"] = "not-a-clock"
        elif bad == "missing_publication":
            body["records"][0].pop("source_published_at")
        elif bad == "future_publication":
            body["records"][0]["source_published_at"] = "2021-01-01T00:00:00"
        elif bad == "missing_known":
            body["records"][0]["known_at"] = None
        elif bad == "body_cutoff":
            body["known_cutoff"] = "2026-09-09T20:00:00"
        elif bad == "errors":
            body["errors"] = [None]
        else:
            body["receipt_refs"] = ["bad"]
        payload = encode(body)
        index = replace(index, payload=payload, evidence_hash=digest(payload))
    result = identity.identity_pair_gate(index, codes=("600001",),
        prediction_at=datetime(2026, 9, 7, 20), outcome_day=date(2026, 9, 8), evaluation_as_of=cutoff)
    assert not result["passed"] and result["reasons"]


@pytest.mark.asyncio
async def test_forked_revisions_with_equal_known_clock_are_unknown_even_if_disjoint(env):
    append(env, event(env))
    boundary = env[1][0] + timedelta(days=1)
    append(env, event(env, "fork-a", end=boundary, supersedes=("e1",)),
           event(env, "fork-b", start=boundary, supersedes=("e1",)))
    index = await prepare(env)
    assert resolve(index, "600001", env[1][0], env[1][0])[2] == ["identity_revision_fork"]


@pytest.mark.asyncio
async def test_parent_and_revision_same_known_clock_rejected(env):
    append(env, event(env), event(env, "same-clock-child", supersedes=("e1",)))
    index = await prepare(env)
    assert resolve(index, "600001", env[1][0], env[1][0])[2] == ["identity_superseded_missing_or_clock_conflict"]


@pytest.mark.asyncio
async def test_rejected_material_hash_binds_actual_damaged_bytes(env):
    root, box, policy = env
    first = append(env, event(env))
    archive = MaterialArchive(root)
    receipt = decode(archive.read_bytes(first["receipt_ref"]))
    record = decode(archive.read_bytes(receipt["record_ref"]))
    path = root / record["raw_ref"]["path"]
    original = path.read_bytes()
    box[0] += timedelta(seconds=1)
    fixed = box[0]
    path.write_bytes(b"x" + original[1:])
    first_index = await identity.prepare_identity_index(known_cutoff=fixed, archive_root=root, policy=policy)
    path.write_bytes(b"y" + original[1:])
    second_index = await identity.prepare_identity_index(known_cutoff=fixed, archive_root=root, policy=policy)
    a = decode(first_index.payload)["errors"][0]["observed_objects"][-1]
    b = decode(second_index.payload)["errors"][0]["observed_objects"][-1]
    assert a["claimed_sha256"] == b["claimed_sha256"]
    assert a["observed_sha256"] != b["observed_sha256"]
    assert first_index.evidence_hash != second_index.evidence_hash


@pytest.mark.asyncio
async def test_obviously_future_corrupt_file_does_not_taint_earlier_index(env):
    import os
    root, box, policy = env
    append(env, event(env))
    first = await prepare(env)
    archive = MaterialArchive(root)
    ref = archive.put(b"not-json", area="ready")
    later = first.known_cutoff + timedelta(days=1)
    os.utime(root / ref["path"], (later.timestamp(), later.timestamp()))
    again = await identity.prepare_identity_index(known_cutoff=first.known_cutoff, archive_root=root, policy=policy)
    assert first.evidence_hash == again.evidence_hash
    # Undatable receipt, already physically observable, must remain unknown.
    ref = archive.put(encode({"schema": identity.SCHEMA, "unknown_clock": True}), area="ready")
    bad = await identity.prepare_identity_index(known_cutoff=first.known_cutoff, archive_root=root, policy=policy)
    assert decode(bad.payload)["errors"][0]["observed_objects"]


@pytest.mark.asyncio
async def test_source_publication_missing_on_replay_not_silently_null(env):
    root, box, policy = env
    append(env, event(env))
    def broken_parser(raw):
        p = parser(raw)
        p["events"][0].pop("source_published_at")
        return p
    changed_policy = ReviewedSourcePolicy(policy.policy_id, {"fixture:1": broken_parser})
    box[0] += timedelta(seconds=1)
    index = await identity.prepare_identity_index(known_cutoff=box[0], archive_root=root, policy=changed_policy)
    assert "identity_event_fields_missing_or_unknown" in decode(index.payload)["errors"][0]["reason"]


@pytest.mark.asyncio
async def test_replay_source_publication_must_precede_first_receive(env):
    root, box, policy = env
    first = append(env, event(env))
    archive = MaterialArchive(root)
    receipt = decode(archive.read_bytes(first["receipt_ref"]))
    record = decode(archive.read_bytes(receipt["record_ref"]))
    raw = decode(archive.read_bytes(record["raw_ref"]))
    published = datetime.fromisoformat(record["received_at"]) + timedelta(microseconds=1)
    raw["events"][0]["source_published_at"] = published.isoformat()
    record["events"] = raw["events"]
    record["raw_ref"] = archive.put(encode(raw))
    record_ref = archive.put(encode(record))
    box[0] += timedelta(seconds=1)
    archive.put(encode({**receipt, "record_ref": record_ref, "known_at": box[0].isoformat()}), area="ready")
    index = await prepare(env)
    assert any("identity_source_publication_future" in e["reason"] for e in decode(index.payload)["errors"])


@pytest.mark.asyncio
async def test_dedup_earliest_known_independent_of_receipt_iteration_order(env):
    original = event(env, state={**NORMAL, "risk_warning": "ST"})
    append(env, original)
    append(env, event(env, "correct", supersedes=("e1",)))
    append(env, original)
    index = await prepare(env)
    newest_first = sorted(decode(index.payload)["records"], key=lambda r: r["known_at"], reverse=True)
    state, _, errors = identity._resolve(newest_first, "600001", env[1][0],
                                         env[1][0] + timedelta(microseconds=1), env[1][0])
    assert not errors and state["risk_warning"] == "normal"


@pytest.mark.asyncio
async def test_semantic_hash_idempotent_across_later_wall_clock_but_not_new_revision(env):
    append(env, event(env))
    prediction = env[1][0] + timedelta(minutes=1)
    env[1][0] = datetime.combine(prediction.date() + timedelta(days=1), time(16))
    first = await prepare(env)
    g1 = query(first, env, prediction=prediction)
    env[1][0] += timedelta(seconds=10)
    second = await prepare(env)
    g2 = query(second, env, prediction=prediction)
    assert g1["passed"] and g2["passed"]
    assert first.payload_hash != second.payload_hash
    assert first.evidence_hash == second.evidence_hash
    assert g1["evidence_hash"] == g2["evidence_hash"]
    assert g1["query_clocks"] != g2["query_clocks"]
    append(env, event(env, "later-correction", state={**NORMAL, "risk_warning": "ST"}, supersedes=("e1",)))
    third = await prepare(env)
    g3 = query(third, env, prediction=prediction)
    assert not g3["passed"]
    assert third.evidence_hash != second.evidence_hash
    assert g3["evidence_hash"] != g2["evidence_hash"]


@pytest.mark.asyncio
async def test_unknown_or_rejected_unchanged_material_hash_is_wall_clock_idempotent(env):
    root, box, policy = env
    result = identity.append_identity_evidence(b"unknown-current-status", source="StockTag", source_version="current",
                                              archive_root=root)
    box[0] = max(box[0], datetime.now()) + timedelta(days=1)
    box[0] = box[0].replace(hour=16)
    first = await prepare(env)
    fixed_prediction = datetime.combine(box[0].date() - timedelta(days=1), time(20))
    g1 = query(first, env, prediction=fixed_prediction)
    second = await prepare(env)
    g2 = query(second, env, prediction=fixed_prediction)
    assert not g1["passed"] and not g2["passed"]
    assert first.evidence_hash == second.evidence_hash
    assert g1["evidence_hash"] == g2["evidence_hash"]


def test_payload_hash_protects_cutoff_even_when_semantic_hash_unchanged():
    from dataclasses import replace
    cutoff = datetime(2026, 9, 8, 20)
    first = fixture_index(known_cutoff=cutoff)
    body = decode(first.payload)
    body["known_cutoff"] = (cutoff + timedelta(seconds=1)).isoformat()
    altered = encode(body)
    assert identity._semantic_index_hash(body) == first.evidence_hash
    corrupt = replace(first, payload=altered)
    gate = identity.identity_pair_gate(corrupt, codes=("600001",), prediction_at=datetime(2026, 9, 7, 20),
                                      outcome_day=date(2026, 9, 8), evaluation_as_of=cutoff)
    assert gate["passed"] is False
    assert "identity_index_payload_hash_mismatch" in gate["reasons"]


@pytest.mark.asyncio
async def test_crossing_first_known_time_changes_semantics_not_empty_query_clock(env):
    root, box, policy = env
    before = await identity.prepare_identity_index(known_cutoff=box[0], archive_root=root, policy=policy)
    first = append(env, event(env))
    after = await prepare(env)
    assert before.evidence_hash != after.evidence_hash
    still = await prepare(env)
    assert still.evidence_hash == after.evidence_hash
    assert still.payload_hash != after.payload_hash
