"""Frozen paired-label evidence over shared M1 raw envelopes; no mutable fallback.

Only reviewed executable parsers can establish finality. The default is empty.
This forward-adjusted profile is not a raw-price markout/crosscheck contract.
"""
from __future__ import annotations

import asyncio
import os
import re
import stat
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from types import SimpleNamespace

PROFILE = "promotion_paired_label_forward_adjusted_prediction_cutoff_v2"
SCHEMA = "promotion_outcome_receipt_v1"


def _m():
    # Avoid eager modeling.__init__ -> training -> ledger consumer imports.
    from app.promotion.modeling import daily_materials
    return daily_materials


def _now():
    return datetime.now()


def _archives(root, policy):
    m = _m()
    if root is None:
        from app.config.settings import settings
        root = settings.PROMOTION_DAILY_MATERIAL_DIR
    root = Path(root)
    policy = policy or m.ReviewedSourcePolicy("no_reviewed_outcome_source_v1")
    return (m.MaterialArchive(root, policy=policy),
            m.MaterialArchive(root / "outcome_receipts", policy=policy))


def append_outcome_evidence(*, close_ref, pool_ref, archive_root=None, policy=None):
    """Publish references to shared envelopes, never a caller-supplied known clock.

    Publication is not readiness. Invalid/unknown material remains visible and
    blocks research; prepare replays reviewed parsers against physical raw bytes.
    """
    m = _m()
    archive, receipts = _archives(archive_root, policy)
    refs = m.decode(m.encode({"close_ref": close_ref, "pool_ref": pool_ref}))
    # At least require actual valid referenced envelopes before publication.
    for ref in refs.values():
        archive.read_bytes(ref)
    body = {"schema": SCHEMA, "profile": PROFILE, "policy_id": archive.policy.policy_id,
            **refs, "known_at": _now().isoformat()}
    return receipts.put(m.encode(body), area="ready")


def _observe(archive, ref, cutoff, observations, cache=None):
    m = _m()
    path = archive._path(ref)
    cache_key = (str(path), ref["sha256"], ref["size"])
    if cache is not None and cache_key in cache:
        raw, available, observation = cache[cache_key]
        observations.append(dict(observation))
        return raw, available
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("material_not_regular")
        available = datetime.fromtimestamp(info.st_mtime)
        if available > cutoff:
            raise ValueError("material_physical_clock_after_cutoff")
        raw = stream.read(m.MAX_BYTES + 1)
    observations.append({"path": ref["path"], "claimed_sha256": ref["sha256"],
                         "observed_sha256": m.digest(raw), "file_size": info.st_size,
                         "observed_bytes": len(raw), "complete": len(raw) == info.st_size})
    if len(raw) != ref["size"] or m.digest(raw) != ref["sha256"]:
        raise ValueError("material_physical_sha_or_size_mismatch")
    if cache is not None:
        cache[cache_key] = (raw, available, dict(observations[-1]))
    return raw, available


def _load(archive, ref, kind, known, observations, read_cutoff, cache):
    m = _m()
    raw_envelope, physical = _observe(archive, ref, read_cutoff, observations, cache)
    envelope = m.decode(raw_envelope)
    if not isinstance(envelope, dict) or envelope.get("schema_version") != m.MATERIAL_SCHEMA:
        raise ValueError("material_schema_mismatch")
    raw, raw_physical = _observe(archive, envelope["raw_ref"], read_cutoff, observations, cache)
    source, version = envelope.get("source"), envelope.get("source_version")
    if not isinstance(source, str) or not isinstance(version, str):
        raise ValueError("source_identity_missing")
    parser = archive.policy.validators.get(source + ":" + version)
    if parser is None or envelope.get("provenance") != "forward_response":
        raise ValueError("source_protocol_unreviewed_or_observed_now")
    parsed = m.decode(m.encode(parser(raw)))
    if not isinstance(parsed, dict):
        raise ValueError("parser_not_object")
    loaded = {"manifest": envelope, "parsed": parsed, "status": "verified_protocol",
              "ref": ref, "reasons": []}

    class OwnedArchive:
        def read(self, ignored):
            return loaded

        def now(self):
            return known

    # The SAME M1 envelope, coverage, source, unit and finality checks; no IO here.
    from app.promotion.modeling.daily_materialization import _validated
    _validated(OwnedArchive(), ref, kind)
    published = m.clock(parsed.get("source_published_at"))
    source_at = m.clock(parsed.get("source_quote_at"))
    received = m.clock(envelope.get("received_at"))
    archived = m.clock(envelope.get("archive_created_at"))
    universe = m.clock(parsed.get("universe_frozen_at"))
    day = date.fromisoformat(parsed["trade_date"])
    if (published is None or not source_at <= published <= received
            or not max(physical, raw_physical, archived) <= known
            or universe > datetime.combine(day, time(9, 15))):
        raise ValueError("publication_or_universe_clock_unproven")
    finality = parsed.get("finality_evidence")
    if (not isinstance(finality, dict)
            or any(not isinstance(finality.get(k), str) or not finality[k].strip()
                   for k in ("kind", "reference"))):
        raise ValueError("explicit_finality_evidence_missing")
    return {"ref": ref, "raw_ref": envelope["raw_ref"], "envelope": envelope,
            "parsed": parsed, "available_at": physical.isoformat(),
            "raw_available_at": raw_physical.isoformat()}


def _semantic(body):
    m = _m()
    return m.digest(m.encode({"contract": PROFILE,
                             "material": {k: v for k, v in body.items() if k != "known_cutoff"}}))


@dataclass(frozen=True)
class OutcomeIndex:
    payload: bytes
    known_cutoff: datetime
    evidence_hash: str
    payload_hash: str


def _prepare(*, known_cutoff, archive_root=None, policy=None):
    m = _m()
    if m.clock(known_cutoff) is None or known_cutoff > _now():
        raise ValueError("invalid_or_future_known_cutoff")
    archive, receipts = _archives(archive_root, policy)
    records, errors, cache = [], [], {}
    directory = receipts._safe(receipts.root / "ready")
    for path in sorted(directory.glob("*.blob")) if directory.exists() else ():
        observations = []
        ref = {"path": "ready/" + path.name, "sha256": path.stem, "size": 0}
        try:
            receipts._safe(path)
            info = path.lstat()
            if stat.S_ISREG(info.st_mode) and datetime.fromtimestamp(info.st_mtime) > known_cutoff:
                continue
            ref["size"] = info.st_size
            raw, physical = _observe(receipts, ref, known_cutoff, observations)
            body = m.decode(raw)
            if not isinstance(body, dict) or body.get("schema") != SCHEMA or body.get("profile") != PROFILE:
                raise ValueError("receipt_schema_invalid")
            declared = m.clock(body.get("known_at"))
            if declared is None:
                raise ValueError("receipt_known_clock_missing")
            if declared > known_cutoff:
                continue
            known = max(declared, physical)
            if body.get("policy_id") != archive.policy.policy_id:
                raise ValueError("receipt_policy_mismatch")
            close = _load(archive, body["close_ref"], "close", known, observations, known_cutoff, cache)
            pool = _load(archive, body["pool_ref"], "pool", known, observations, known_cutoff, cache)
            if (close["parsed"]["trade_date"] != pool["parsed"]["trade_date"]
                    or set(close["parsed"]["universe_codes"]) != set(pool["parsed"]["universe_codes"])):
                raise ValueError("close_pool_session_or_universe_mismatch")
            records.append({"known_at": known.isoformat(), "receipt": ref,
                            "declared_known_at": declared.isoformat(), "receipt_available_at": physical.isoformat(),
                            "trade_date": close["parsed"]["trade_date"],
                            "close": close, "pool": pool})
        except (ValueError, TypeError, KeyError, AttributeError, OSError, OverflowError) as exc:
            errors.append({"ref": ref, "reason": str(exc), "observed_objects": observations})
    body = {"profile": PROFILE, "policy_id": archive.policy.policy_id,
            "known_cutoff": known_cutoff.isoformat(), "records": records, "errors": errors}
    payload = m.encode(body)
    return OutcomeIndex(payload, known_cutoff, _semantic(body), m.digest(payload))


async def prepare_outcome_index(*, known_cutoff, archive_root=None, policy=None):
    """Lock and verify all visible receipts/raw once in a worker; no ORM or writes."""
    return await asyncio.to_thread(_prepare, known_cutoff=known_cutoff,
                                   archive_root=archive_root, policy=policy)


def _ref_contract(ref, area="objects"):
    m = _m()
    if (not isinstance(ref, dict) or set(ref) != {"path", "sha256", "size"}
            or not isinstance(ref["sha256"], str) or not re.fullmatch("[a-f0-9]{64}", ref["sha256"])
            or ref["path"] != area + "/" + ref["sha256"] + ".blob"
            or type(ref["size"]) is not int or not 0 < ref["size"] <= m.MAX_BYTES):
        raise ValueError("frozen_ref_contract_invalid")


def _body_contract(body, cutoff):
    """Revalidate owned JSON structure, not a claimed prepared-object status."""
    m = _m()
    if (not isinstance(body, dict)
            or set(body) != {"profile", "policy_id", "known_cutoff", "records", "errors"}
            or body["profile"] != PROFILE or body["known_cutoff"] != cutoff.isoformat()
            or not isinstance(body["policy_id"], str) or not body["policy_id"].strip()
            or not isinstance(body["records"], list) or not isinstance(body["errors"], list)):
        raise ValueError("outcome_index_structure_invalid")
    for error in body["errors"]:
        if (not isinstance(error, dict) or set(error) != {"ref", "reason", "observed_objects"}
                or not isinstance(error["reason"], str) or not error["reason"]
                or not isinstance(error["observed_objects"], list)):
            raise ValueError("outcome_error_structure_invalid")
    from app.promotion.modeling.daily_materialization import _validated
    for record in body["records"]:
        if not isinstance(record, dict) or set(record) != {
                "known_at", "receipt", "declared_known_at", "receipt_available_at", "trade_date", "close", "pool"}:
            raise ValueError("outcome_record_structure_invalid")
        known, declared, available = (m.clock(record[k]) for k in
                                     ("known_at", "declared_known_at", "receipt_available_at"))
        if any(c is None for c in (known, declared, available)) or not max(declared, available) == known <= cutoff:
            raise ValueError("outcome_record_clock_invalid")
        _ref_contract(record["receipt"], "ready")
        day = date.fromisoformat(record["trade_date"])
        for kind in ("close", "pool"):
            item = record[kind]
            if not isinstance(item, dict) or set(item) != {
                    "ref", "raw_ref", "envelope", "parsed", "available_at", "raw_available_at"}:
                raise ValueError("outcome_material_structure_invalid")
            _ref_contract(item["ref"])
            _ref_contract(item["raw_ref"])
            envelope, parsed = item["envelope"], item["parsed"]
            if (not isinstance(envelope, dict) or not isinstance(parsed, dict)
                    or envelope.get("schema_version") != m.MATERIAL_SCHEMA
                    or envelope.get("provenance") != "forward_response"
                    or envelope.get("raw_ref") != item["raw_ref"]
                    or parsed.get("trade_date") != str(day)):
                raise ValueError("outcome_envelope_structure_invalid")

            class OwnedArchive:
                def read(self, ignored):
                    return {"manifest": envelope, "parsed": parsed, "ref": item["ref"],
                            "status": "verified_protocol", "reasons": []}

                def now(self):
                    return known

            _validated(OwnedArchive(), item["ref"], kind)
            clocks = [m.clock(item["available_at"]), m.clock(item["raw_available_at"]),
                      m.clock(parsed.get("source_published_at"))]
            if (any(c is None for c in clocks) or max(clocks[:2]) > known
                    or not m.clock(parsed["source_quote_at"]) <= clocks[2] <= m.clock(envelope["received_at"])
                    or m.clock(parsed["universe_frozen_at"]) > datetime.combine(day, time(9, 15))):
                raise ValueError("frozen_material_clock_invalid")
            evidence = parsed.get("finality_evidence")
            if not isinstance(evidence, dict) or any(
                    not isinstance(evidence.get(k), str) or not evidence[k].strip() for k in ("kind", "reference")):
                raise ValueError("frozen_finality_evidence_invalid")
        if set(record["close"]["parsed"]["universe_codes"]) != set(record["pool"]["parsed"]["universe_codes"]):
            raise ValueError("frozen_session_universe_mismatch")


def outcome_pair_gate(index, *, codes: tuple, prediction_day: date, prediction_at: datetime,
                      outcome_day: date, evaluation_as_of: datetime):
    """Caller supplies the registered actual T+1; never infer it from available K."""
    m = _m()
    reasons, per_code = [], []
    actual_semantic, diagnostic_cutoff = "invalid_handle", None
    try:
        if not isinstance(index, OutcomeIndex) or not isinstance(index.payload, bytes):
            raise ValueError("outcome_index_type_invalid")
        actual_semantic = m.digest(index.payload)
        if m.clock(index.known_cutoff) is None:
            raise ValueError("outcome_index_cutoff_invalid")
        diagnostic_cutoff = index.known_cutoff.isoformat()
        body = m.decode(index.payload)
        actual_semantic = _semantic(body)
        if (m.digest(index.payload) != index.payload_hash or actual_semantic != index.evidence_hash
                or body.get("profile") != PROFILE
                or body.get("known_cutoff") != index.known_cutoff.isoformat()
                or not isinstance(body.get("records"), list) or not isinstance(body.get("errors"), list)
                or not isinstance(body.get("policy_id"), str)):
            raise ValueError("outcome_index_integrity_invalid")
        _body_contract(body, index.known_cutoff)
        if (m.clock(evaluation_as_of) is None or m.clock(index.known_cutoff) is None
                or not evaluation_as_of <= index.known_cutoff <= _now()):
            raise ValueError("invalid_or_future_evaluation_clock")
        if (not all(isinstance(d, date) and not isinstance(d, datetime) for d in (prediction_day, outcome_day))
                or not prediction_day < outcome_day
                or evaluation_as_of < datetime.combine(outcome_day, time(15, 10))):
            raise ValueError("outcome_session_not_completed")
        if (m.clock(prediction_at) is None or prediction_at.date() != prediction_day
                or prediction_at > evaluation_as_of):
            raise ValueError("invalid_prediction_clock")
        from app.promotion.modeling.daily_materialization import _codes
        if not isinstance(codes, tuple) or not codes:
            raise ValueError("candidate_codes_required")
        _codes(list(codes))
        if body["errors"]:
            raise ValueError("visible_material_rejected")
        pair = []
        for day, visible_at in ((prediction_day, prediction_at), (outcome_day, evaluation_as_of)):
            records = [r for r in body["records"] if r["trade_date"] == day.isoformat()
                       and m.clock(r["known_at"]) <= visible_at]
            if not records:
                raise ValueError("session_material_missing")
            latest = max(r["known_at"] for r in records)
            candidates = [r for r in records if r["known_at"] == latest]
            if len(candidates) != 1:
                raise ValueError("same_known_receipt_conflict")
            pair.append(candidates[0])
        bases = {tuple(r[k]["parsed"][n] for n in ("price_basis", "adjustment_basis",
                  "adjustment_version", "volume_unit", "amount_unit"))
                 for r in pair for k in ("close", "pool")}
        universes = {tuple(sorted(r[k]["parsed"]["universe_codes"])) for r in pair for k in ("close", "pool")}
        if len(bases) != 1 or len(universes) != 1:
            raise ValueError("paired_basis_or_universe_revision_mismatch")
        before, after = [{r["code"]: r for r in batch["close"]["parsed"]["rows"]} for batch in pair]
        from app.promotion.outcome_evidence import formal_outcome_bar_error
        # Validate the entire frozen universe, never only the chosen candidates.
        for code in next(iter(universes)):
            a, b = before[code], after[code]
            error = formal_outcome_bar_error(
                SimpleNamespace(**{**a, "source": pair[0]["close"]["envelope"]["source"]}),
                SimpleNamespace(**{**b, "source": pair[1]["close"]["envelope"]["source"]}))
            if error:
                raise ValueError(error)
        if not set(codes) <= set(before):
            raise ValueError("candidate_outside_frozen_universe")
        pools = [set(r["code"] for r in p["pool"]["parsed"]["rows"]) for p in pair]
        refs = [{"receipt": p["receipt"], "close": p["close"]["ref"], "pool": p["pool"]["ref"],
                 "close_raw": p["close"]["raw_ref"], "pool_raw": p["pool"]["raw_ref"]} for p in pair]
        basis = {k: pair[0]["close"]["parsed"][k] for k in (
            "price_basis", "adjustment_basis", "adjustment_version", "volume_unit", "amount_unit")}
        for code in codes:
            per_code.append({"code": code, "status": "verified", "reasons": [],
                             "prediction_limit_up": code in pools[0], "outcome_limit_up": code in pools[1],
                             "before_close": before[code]["close"], "after_close": after[code]["close"],
                             "after_prev_close": after[code]["prev_close"], "refs": refs, **basis})
    except (ValueError, KeyError, TypeError, AttributeError, OverflowError) as exc:
        reasons.append(str(exc))
        per_code = [{"code": c, "status": "unknown", "reasons": list(reasons),
                     "prediction_limit_up": None, "outcome_limit_up": None, "before_close": None,
                     "after_close": None, "after_prev_close": None, "refs": [],
                     "price_basis": None, "adjustment_basis": None, "adjustment_version": None,
                     "volume_unit": None, "amount_unit": None}
                    for c in codes] if isinstance(codes, tuple) else []
    result = {"passed": not reasons, "profile": PROFILE, "reasons": reasons, "per_code": per_code}
    result["evidence_hash"] = m.digest(m.encode({"gate": result, "material_hash": actual_semantic,
                                               "prediction_day": str(prediction_day), "prediction_at": str(prediction_at),
                                               "outcome_day": str(outcome_day)}))
    result["query_clocks"] = {"known_cutoff": diagnostic_cutoff,
                              "evaluation_as_of": str(evaluation_as_of)}
    return result
