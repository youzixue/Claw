"""Append-only research identity evidence. No ORM/current tags/source collection.

Reviewed parsers are trusted protocol code, never booleans supplied as metadata.
The default policy has no validators: actual historical identity remains unknown.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
import os
import stat

# Keep modeling imports lazy: modeling.__init__ imports training -> ledger ->
# shadow, which consumes this module. No package bootstrap in this import path.
def _materials():
    from app.promotion.modeling import daily_materials
    return daily_materials


def clock(value):
    return _materials().clock(value)


def decode(raw):
    return _materials().decode(raw)


def encode(value):
    return _materials().encode(value)


def digest(raw):
    return _materials().digest(raw)

PROFILE = "promotion_identity_mainboard_security_session_v2"
SCHEMA = "promotion_identity_events_v2"
_EVENT_KEYS = {"event_id", "code", "instrument_id", "effective_from", "effective_to",
               "effective_verified", "state", "supersedes", "source_published_at"}
_RECORD_META = {"known_at", "received_at", "observed_at", "archive_created_at",
                "receipt_hash", "record_hash", "raw_hash"}
DEFAULT_ARCHIVE_ROOT = Path(__file__).resolve().parents[2] / "outputs" / "promotion_identity_archive"
_FIELDS = {
    "risk_warning": {"normal", "ST", "*ST", "unknown"},
    "listing_state": {"listed", "delisting_period", "delisted", "unknown"},
    "delisting_risk": {"clear", "warned", "unknown"},
    "suspension_state": {"trading", "suspended", "unknown"},
    "board": {"main", "gem", "star", "bse", "unknown"},
}
_NORMAL = {"risk_warning": "normal", "listing_state": "listed", "delisting_risk": "clear",
           "suspension_state": "trading", "board": "main"}


def _now():
    return datetime.now()


def _archive(root, policy):
    MaterialArchive = _materials().MaterialArchive
    ReviewedSourcePolicy = _materials().ReviewedSourcePolicy
    return MaterialArchive(root if root is not None else DEFAULT_ARCHIVE_ROOT,
                           policy=policy or ReviewedSourcePolicy("no_reviewed_identity_source_v1"),
                           clock=_now)


def _code(value):
    return isinstance(value, str) and len(value) == 6 and all("0" <= c <= "9" for c in value)


def _datetime(value):
    return isinstance(value, datetime) and clock(value) is not None


def _events(parsed):
    if not isinstance(parsed, dict) or parsed.get("identity_verified") is not True:
        raise ValueError("identity_protocol_unverified")
    rows = parsed.get("events")
    if not isinstance(rows, list) or not rows:
        raise ValueError("identity_events_missing")
    ids = set()
    for row in rows:
        if not isinstance(row, dict) or not _code(row.get("code")):
            raise ValueError("identity_event_code_invalid")
        if set(row) != _EVENT_KEYS:
            raise ValueError("identity_event_fields_missing_or_unknown")
        instrument = row.get("instrument_id")
        if not isinstance(instrument, str) or not instrument.strip() or len(instrument) > 128 or not instrument.isascii():
            raise ValueError("identity_instrument_id_missing_or_invalid")
        event_id = row.get("event_id")
        if not isinstance(event_id, str) or not event_id or event_id in ids:
            raise ValueError("identity_event_id_duplicate_or_missing")
        ids.add(event_id)
        start, end = clock(row.get("effective_from")), clock(row.get("effective_to"))
        if start is None or end is None or start >= end or row.get("effective_verified") is not True:
            raise ValueError("identity_effective_interval_unproven")
        state = row.get("state")
        if not isinstance(state, dict) or set(state) != set(_FIELDS):
            raise ValueError("identity_required_fields_missing")
        for name, allowed in _FIELDS.items():
            if not isinstance(state[name], str) or state[name] not in allowed:
                raise ValueError("identity_state_invalid")
        parents = row.get("supersedes", [])
        if (not isinstance(parents, list) or any(not isinstance(p, str) or not p or p == event_id for p in parents)
                or len(set(parents)) != len(parents)):
            raise ValueError("identity_supersedes_invalid")
        published = row.get("source_published_at")
        if published is not None and clock(published) is None:
            raise ValueError("identity_source_publication_clock_invalid")
    return rows


def append_identity_evidence(raw_bytes, *, source, source_version, archive_root=None, policy=None):
    """Persist received-now evidence. No effective/known clocks accepted as kwargs.

    A parser may preserve genuinely old effective dates. Known time is always
    actual durable ingestion now. Unreviewed material is archived but not usable.
    """
    archive = _archive(archive_root, policy)
    received = _now()
    raw_ref = archive.put(raw_bytes)
    reasons, events = [], []
    parser = archive.policy.validators.get(f"{source}:{source_version}")
    if parser is None:
        reasons.append("identity_source_protocol_unreviewed")
    else:
        try:
            parsed = decode(encode(parser(raw_bytes)))
            events = _events(parsed)
            if parsed.get("source") != source or parsed.get("source_version") != source_version:
                raise ValueError("identity_source_mismatch")
            if any(clock(e["source_published_at"]) > received for e in events if e.get("source_published_at") is not None):
                raise ValueError("identity_source_publication_future")
        except (ValueError, KeyError, TypeError) as exc:
            reasons.append(str(exc))
            events = []
    record = {"schema": SCHEMA, "source": source, "source_version": source_version,
              "policy_id": archive.policy.policy_id, "raw_ref": raw_ref,
              "received_at": received.isoformat(), "observed_at": _now().isoformat(),
              "archive_created_at": _now().isoformat(),
              "events": events, "reasons": reasons}
    record_ref = archive.put(encode(record))
    receipt = {"schema": SCHEMA, "record_ref": record_ref, "known_at": _now().isoformat()}
    receipt_ref = archive.put(encode(receipt), area="ready")  # independent identity root only
    return {"status": "unknown" if reasons else "recorded", "receipt_ref": receipt_ref, "reasons": reasons}


@dataclass(frozen=True)
class IdentityIndex:
    payload: bytes
    known_cutoff: datetime
    evidence_hash: str
    payload_hash: str = ""

    def __post_init__(self):
        if not self.payload_hash and isinstance(self.payload, bytes):
            object.__setattr__(self, "payload_hash", digest(self.payload))


def _semantic_index_hash(body):
    # Only the query boundary is excluded. Every source known/effective clock,
    # observed rejected byte hash, policy, revision and reference is retained.
    material = {k: v for k, v in body.items() if k != "known_cutoff"} if isinstance(body, dict) else body
    return digest(encode({"hash_contract": "identity_material_semantics_v1", "material": material}))


def _observed_index_semantics(index):
    if not isinstance(index, IdentityIndex) or not isinstance(index.payload, bytes):
        return None
    try:
        return _semantic_index_hash(decode(index.payload))
    except (ValueError, TypeError):
        return digest(index.payload)  # Unparseable material cannot claim a query clock.


def _read_observed(archive, ref, observations, cutoff):
    """Read once with M1 path rules; hash ACTUAL bounded bytes even on SHA failure.

    Never follow symlinks or read content that is physically later than cutoff.
    A rejected claim cannot substitute its filename SHA for observed bytes.
    """
    path = archive._path(ref)
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("identity_object_not_regular")
        if datetime.fromtimestamp(info.st_mtime) > cutoff:
            raise ValueError("identity_object_not_available_at_cutoff")
        limit = _materials().MAX_BYTES
        raw = stream.read(limit + 1)
    actual_hash = digest(raw)
    observations.append({"path": ref["path"], "claimed_sha256": ref["sha256"],
                         "observed_sha256": actual_hash, "observed_bytes": len(raw),
                         "file_size": info.st_size, "complete": len(raw) == info.st_size})
    if info.st_size != ref["size"] or len(raw) > limit or actual_hash != ref["sha256"]:
        raise ValueError("identity_object_physical_hash_or_size_mismatch")
    return raw


def _prepare(*, known_cutoff, archive_root, policy):
    if not _datetime(known_cutoff) or known_cutoff > _now():
        raise ValueError("identity_cutoff_missing_or_future")
    archive = _archive(archive_root, policy)
    records, errors, observed_refs = [], [], []
    for path in sorted(archive._safe(archive.root / "ready").glob("*.blob")):
        ref = {"path": "ready/" + path.name, "sha256": path.stem, "size": 0}
        observations = []
        try:
            archive._safe(path)
            info = path.lstat()
            if stat.S_ISREG(info.st_mode) and datetime.fromtimestamp(info.st_mtime) > known_cutoff:
                continue  # Clearly future physical files cannot taint an earlier view.
            ref["size"] = info.st_size
            receipt = decode(_read_observed(archive, ref, observations, known_cutoff))
            if not isinstance(receipt, dict) or receipt.get("schema") != SCHEMA:
                raise ValueError("identity_receipt_schema_invalid")
            declared = clock(receipt.get("known_at"))
            if declared is None:
                raise ValueError("identity_known_clock_missing")
            known = max(declared, archive.available_at(ref))
            if known > known_cutoff:
                continue
            observed_refs.append(ref["sha256"])
            record_ref = receipt["record_ref"]
            record = decode(_read_observed(archive, record_ref, observations, known_cutoff))
            if not isinstance(record, dict) or record.get("schema") != SCHEMA:
                raise ValueError("identity_record_schema_invalid")
            raw = _read_observed(archive, record["raw_ref"], observations, known_cutoff)
            received, observed, created = (clock(record.get(k)) for k in ("received_at", "observed_at", "archive_created_at"))
            if (received is None or observed is None or created is None or not received <= observed <= created <= declared
                    or archive.available_at(record_ref) > known or archive.available_at(record["raw_ref"]) > known):
                raise ValueError("identity_physical_clock_invalid")
            parser = archive.policy.validators.get(f'{record["source"]}:{record["source_version"]}')
            if parser is None or record.get("policy_id") != archive.policy.policy_id or record.get("reasons"):
                raise ValueError("identity_source_protocol_unreviewed")
            parsed = decode(encode(parser(raw)))
            events = _events(parsed)
            if (events != record["events"] or parsed.get("source") != record["source"]
                    or parsed.get("source_version") != record["source_version"]):
                raise ValueError("identity_raw_replay_mismatch")
            for e in events:
                published = clock(e.get("source_published_at"))
                if published is not None and published > received:
                    raise ValueError("identity_source_publication_future")
                records.append({**e, "known_at": known.isoformat(),
                                "received_at": received.isoformat(), "observed_at": observed.isoformat(),
                                "archive_created_at": created.isoformat(), "receipt_hash": ref["sha256"],
                                "record_hash": record_ref["sha256"], "raw_hash": record["raw_ref"]["sha256"]})
        except (ValueError, KeyError, TypeError, OSError) as exc:
            errors.append({"ref": ref["sha256"], "reason": str(exc), "observed_objects": observations})
    body = {"profile": PROFILE, "policy_id": archive.policy.policy_id,
            "known_cutoff": known_cutoff.isoformat(), "records": records, "errors": errors,
            "receipt_refs": observed_refs}
    payload = encode(body)
    return IdentityIndex(payload, known_cutoff, _semantic_index_hash(body), digest(payload))


async def prepare_identity_index(*, known_cutoff, archive_root=None, policy=None):
    """Batch lock physical evidence on a worker thread, without ORM or writes."""
    return await asyncio.to_thread(_prepare, known_cutoff=known_cutoff, archive_root=archive_root, policy=policy)


def _resolve(records, code, start, end, known_at):
    visible = [r for r in records if r["code"] == code and clock(r["known_at"]) <= known_at]
    by_id = {}
    for r in visible:
        key = r["event_id"]
        if key in by_id:
            # Identical re-ingestion is harmless; differing identity is ambiguous.
            old = {k: v for k, v in by_id[key].items() if k not in _RECORD_META}
            new = {k: v for k, v in r.items() if k not in _RECORD_META}
            if old != new:
                return None, [], ["identity_conflicting_event_id"]
            if clock(r["known_at"]) < clock(by_id[key]["known_at"]):
                by_id[key] = r
            continue
        by_id[key] = r
    superseded = set()
    children = {}
    for r in by_id.values():
        for parent in r.get("supersedes", []):
            old = by_id.get(parent)
            if old is None or clock(old["known_at"]) >= clock(r["known_at"]):
                return None, [], ["identity_superseded_missing_or_clock_conflict"]
            children.setdefault(parent, set()).add(r["event_id"])
            if len(children[parent]) > 1:
                return None, [], ["identity_revision_fork"]
            superseded.add(parent)
    active = [r for key, r in by_id.items() if key not in superseded
              and clock(r["effective_from"]) < end and clock(r["effective_to"]) > start]
    boundaries = sorted({start, end, *[max(start, clock(r["effective_from"])) for r in active],
                          *[min(end, clock(r["effective_to"])) for r in active]})
    refs, states = set(), []
    for left, right in zip(boundaries, boundaries[1:]):
        covering = [r for r in active if clock(r["effective_from"]) <= left and clock(r["effective_to"]) >= right]
        if len(covering) != 1:
            return None, sorted(refs), ["identity_interval_gap" if not covering else "identity_interval_conflict"]
        row = covering[0]
        refs.update((row["receipt_hash"], row["record_hash"], row["raw_hash"]))
        states.append({**row["state"], "instrument_id": row["instrument_id"]})
    if not states or any(s != states[0] for s in states):
        return None, sorted(refs), ["identity_intraday_change"]
    if any(v == "unknown" for v in states[0].values()):
        return states[0], sorted(refs), ["identity_required_field_unknown"]
    return states[0], sorted(refs), []


def _index_body(index):
    if not isinstance(index.payload, bytes):
        raise ValueError("identity_index_structure_invalid")
    if digest(index.payload) != index.payload_hash:
        raise ValueError("identity_index_payload_hash_mismatch")
    body = decode(index.payload)
    if _semantic_index_hash(body) != index.evidence_hash:
        raise ValueError("identity_index_hash_mismatch")
    if (not isinstance(body, dict) or body.get("profile") != PROFILE
            or body.get("known_cutoff") != index.known_cutoff.isoformat()
            or not isinstance(body.get("policy_id"), str) or not body["policy_id"].strip()
            or not isinstance(body.get("records"), list)
            or not isinstance(body.get("errors"), list)
            or not isinstance(body.get("receipt_refs"), list)):
        raise ValueError("identity_index_structure_invalid")
    for error in body["errors"]:
        if not isinstance(error, dict) or not isinstance(error.get("ref"), str) or not isinstance(error.get("reason"), str):
            raise ValueError("identity_index_error_structure_invalid")
    for ref in body["receipt_refs"]:
        if not isinstance(ref, str) or len(ref) != 64 or any(c not in "0123456789abcdef" for c in ref):
            raise ValueError("identity_index_ref_invalid")
    for row in body["records"]:
        if not isinstance(row, dict) or set(row) != _EVENT_KEYS | _RECORD_META:
            raise ValueError("identity_index_record_structure_invalid")
        if row["receipt_hash"] not in body["receipt_refs"]:
            raise ValueError("identity_index_receipt_binding_invalid")
        _events({"identity_verified": True, "events": [{k: row[k] for k in _EVENT_KEYS}]})
        received, observed, created, known = (clock(row[k]) for k in ("received_at", "observed_at", "archive_created_at", "known_at"))
        if (any(v is None for v in (received, observed, created, known))
                or not received <= observed <= created <= known <= index.known_cutoff):
            raise ValueError("identity_index_record_clock_invalid")
        published = clock(row["source_published_at"])
        if published is not None and published > received:
            raise ValueError("identity_source_publication_future")
        for name in ("receipt_hash", "record_hash", "raw_hash"):
            value = row[name]
            if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
                raise ValueError("identity_index_ref_invalid")
    return body


def identity_pair_gate(index, *, codes: tuple[str, ...], prediction_at: datetime,
                       outcome_day: date, evaluation_as_of: datetime):
    """Normal-mainboard research profile. Unknown excludes the WHOLE batch.

    Outcome identity must cover auctions+morning and afternoon through the close
    instant. Lunch is not assumed trading, but identities must agree across both
    sessions; a day transition never silently supplies a negative label.
    """
    reasons, per_code = [], []
    valid = (isinstance(index, IdentityIndex) and _datetime(index.known_cutoff)
             and isinstance(codes, tuple) and bool(codes)
             and all(_code(c) for c in codes) and len(set(codes)) == len(codes)
             and _datetime(prediction_at) and _datetime(evaluation_as_of)
             and isinstance(outcome_day, date) and not isinstance(outcome_day, datetime))
    if not valid:
        reasons.append("identity_gate_input_invalid")
    else:
        try:
            body = _index_body(index)
        except (ValueError, TypeError, KeyError, OverflowError) as exc:
            reasons.append(str(exc))
    if not reasons and not prediction_at <= evaluation_as_of <= index.known_cutoff <= _now():
        reasons.append("identity_query_clock_invalid")
    elif not reasons and outcome_day <= prediction_at.date():
        reasons.append("identity_outcome_day_invalid")
    elif not reasons:
        close_end = datetime.combine(outcome_day, time(15)) + timedelta(microseconds=1)
        if evaluation_as_of < close_end:
            reasons.append("identity_outcome_session_incomplete")
    if not reasons:
        if body["errors"]:
            reasons.append("identity_archive_unverified")
        by_code = {}
        for record in body["records"]:
            by_code.setdefault(record["code"], []).append(record)
        for code in sorted(codes):
            records = by_code.get(code, [])
            state, refs, errors = _resolve(records, code, prediction_at, prediction_at + timedelta(microseconds=1), prediction_at)
            prediction_state = state
            all_errors = list(errors) + (["identity_archive_unverified"] if body["errors"] else [])
            outcome_refs, outcome_states = set(), []
            for start, end in ((time(9, 15), time(11, 30)), (time(13), time(15))):
                outcome_state, found, failed = _resolve(records, code, datetime.combine(outcome_day, start),
                    datetime.combine(outcome_day, end) + timedelta(microseconds=1), evaluation_as_of)
                all_errors.extend(failed)
                outcome_refs.update(found)
                outcome_states.append(outcome_state)
            # Identity continuity also covers lunch: it is NOT counted as
            # trading, but a noon suspension/reinstatement round-trip is a real
            # intraday identity change, not a normal full-session observation.
            _, continuity_refs, continuity_errors = _resolve(records, code,
                datetime.combine(outcome_day, time(9, 15)), close_end, evaluation_as_of)
            outcome_refs.update(continuity_refs)
            all_errors.extend(continuity_errors)
            if outcome_states[0] != outcome_states[1]:
                all_errors.append("identity_intraday_change")
            instrument_ids = {s["instrument_id"] for s in [prediction_state, *outcome_states] if s is not None}
            if len(instrument_ids) != 1:
                all_errors.append("identity_instrument_changed_or_missing")
            if prediction_state is not None and {k: v for k, v in prediction_state.items() if k != "instrument_id"} != _NORMAL:
                all_errors.append("identity_prediction_outside_profile")
            if any(s is not None and {k: v for k, v in s.items() if k != "instrument_id"} != _NORMAL for s in outcome_states):
                all_errors.append("identity_outcome_outside_profile")
            all_errors = sorted(set(all_errors))
            per_code.append({"code": code, "status": "unknown" if all_errors else "verified",
                             "reasons": all_errors, "prediction_refs": refs, "outcome_refs": sorted(outcome_refs),
                             "prediction_instrument_id": prediction_state.get("instrument_id") if prediction_state else None,
                             "outcome_instrument_id": outcome_states[0].get("instrument_id") if outcome_states[0] else None})
            reasons.extend(all_errors)
    result = {"passed": not reasons, "profile": PROFILE, "reasons": sorted(set(reasons)), "per_code": per_code}
    result["evidence_hash"] = digest(encode({"gate": result,
        "index_hash": _observed_index_semantics(index),
        "claimed_index_hash": index.evidence_hash if isinstance(index, IdentityIndex) and isinstance(index.evidence_hash, str) else None,
        "prediction_at": str(prediction_at), "outcome_day": str(outcome_day),
        "outcome_session_contract": "0915_113000000001_1300_150000000001_with_lunch_identity_continuity"}))
    result["query_clocks"] = {
        "index_known_cutoff": str(index.known_cutoff) if isinstance(index, IdentityIndex) else None,
        "evaluation_as_of": str(evaluation_as_of)}
    return result
