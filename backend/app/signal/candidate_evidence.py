"""Append owned radar evaluations without changing the compatibility projection.

captured_at is the real local capture clock, not first market availability or
physical COMMIT time. No historical rows are backfilled, no trading authority.
"""
import hashlib
import json
from datetime import date, datetime

from sqlalchemy import select

from app.models.signal import AnomalyCandidateEvidence

PROTOCOL = "anomaly_candidate_evidence_v1"


def _default(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    raise TypeError("candidate evidence requires JSON leaves")


def freeze(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False, default=_default)


async def append_evaluations(db, *, capture_id, trade_date, captured_at, observations):
    """Caller owns transaction/savepoint. Exact retry is idempotent; collision fails."""
    if (not isinstance(captured_at, datetime) or captured_at.tzinfo is not None
            or captured_at.date() != trade_date or not isinstance(capture_id, str)
            or not capture_id or len(capture_id) > 40):
        raise ValueError("invalid prospective capture clock or identity")
    # Serialize before any await; callers cannot mutate an in-flight observation.
    prepared = []
    for observation in observations:
        record_id, code = observation["record_id"], observation["code"]
        if (not isinstance(record_id, str) or not 1 <= len(record_id) <= 30
                or not isinstance(code, str) or len(code) != 6
                or not code.isascii() or not code.isdecimal()):
            raise ValueError("invalid candidate evidence identity")
        body = freeze({
            **observation, "protocol_version": PROTOCOL, "capture_id": capture_id,
            "trade_date": trade_date.isoformat(), "captured_at": captured_at.isoformat(),
            "clock_basis": "local_capture_before_commit", "historical_pit_verified": False,
            "physical_commit_at": None, "trading_authority": False,
        })
        prepared.append((record_id, code, body, hashlib.sha256(body.encode()).hexdigest()))
    if len({row[0] for row in prepared}) != len(prepared):
        raise ValueError("duplicate candidate identity in one capture")
    with db.no_autoflush:
        previous = (await db.execute(select(
            AnomalyCandidateEvidence.record_id, AnomalyCandidateEvidence.payload_hash,
            AnomalyCandidateEvidence.payload_json,
        ).where(AnomalyCandidateEvidence.capture_id == capture_id))).all()
    existing = {row.record_id: row for row in previous}
    inserted = 0
    for record_id, code, body, digest in prepared:
        row = existing.get(record_id)
        if row is not None:
            if row.payload_hash != digest or row.payload_json != body:
                raise ValueError("immutable candidate capture collision")
            continue
        db.add(AnomalyCandidateEvidence(
            capture_id=capture_id, record_id=record_id, code=code, trade_date=trade_date,
            captured_at=captured_at, protocol_version=PROTOCOL,
            payload_hash=digest, payload_json=body))
        inserted += 1
    await db.flush()
    return {"status": "recorded", "capture_id": capture_id, "inserted": inserted,
            "matched": len(prepared)-inserted, "candidate_count": len(prepared),
            "payload_bytes": sum(len(row[2].encode()) for row in prepared),
            "historical_pit_verified": False}
