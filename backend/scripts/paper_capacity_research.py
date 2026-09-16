"""Replay one supplied frozen capacity scope; never connects to a database.

This CLI validates an input declaration and binds its file hash. It does not
authenticate upstream source SHA references; no real-data or matching claim is
made until a caller has provided independently verified frozen materials.
"""
import argparse
from datetime import date, datetime, time
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile

BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.paper.capacity_research import (
    CapacityScope, CapacitySignal, CapacityObservation, CapacityPolicy,
    NonCapacityGuards, run_capacity_study,
)
from app.paper.signal_research import MarkoutPolicy

INPUT_SCHEMA = "paper_capacity_frozen_input_v1"
MAX_INPUT_BYTES = 8 * 1024 * 1024


def decode_input(payload):
    if not isinstance(payload, dict) or payload.get("schema") != INPUT_SCHEMA:
        raise ValueError("unsupported frozen capacity input schema")
    if set(payload) - {"schema", "scope", "signals", "observations", "policy", "as_of"}:
        raise ValueError("unknown top-level fields; outcome data cannot drive allocation")
    scope_fields = dict(payload["scope"])
    scope_fields["trade_date"] = date.fromisoformat(scope_fields["trade_date"])
    for key in ("observed_at", "window_end"):
        scope_fields[key] = datetime.fromisoformat(scope_fields[key])
    scope_fields["encumbered_codes"] = tuple(scope_fields["encumbered_codes"])
    scope = CapacityScope(**scope_fields)
    signals = []
    for raw in payload["signals"]:
        fields = dict(raw)
        for key in ("confirmed_at", "expires_at", "priority_at"):
            fields[key] = datetime.fromisoformat(fields[key])
        signals.append(CapacitySignal(**fields))
    observations = []
    for raw in payload["observations"]:
        fields = dict(raw)
        for key in ("source_at", "received_at", "observed_at", "guard_at"):
            fields[key] = datetime.fromisoformat(fields[key])
        fields["guards"] = NonCapacityGuards(**fields["guards"])
        observations.append(CapacityObservation(**fields))
    policy_fields = dict(payload.get("policy", {}))
    if "fixed_times" in policy_fields:
        policy_fields["fixed_times"] = tuple(time.fromisoformat(t) for t in policy_fields["fixed_times"])
    if "reserve_until" in policy_fields:
        policy_fields["reserve_until"] = time.fromisoformat(policy_fields["reserve_until"])
    if "costs" in policy_fields:
        policy_fields["costs"] = MarkoutPolicy(**policy_fields["costs"])
    as_of = datetime.fromisoformat(payload["as_of"])
    if as_of.tzinfo is not None or as_of > datetime.now():
        raise ValueError("as_of must be a non-future Shanghai local clock")
    return scope, signals, observations, as_of, CapacityPolicy(**policy_fields)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    source = args.input.resolve(strict=True)
    destination = args.output.resolve()
    if not source.is_file() or source.stat().st_size > MAX_INPUT_BYTES:
        raise ValueError("bounded frozen input file required")
    if not destination.is_relative_to((ROOT / "outputs").resolve()) or destination.exists():
        raise ValueError("output must be new and under project outputs")
    # Bounded read in addition to stat protects against accidental source growth.
    with source.open("rb") as stream:
        raw = stream.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        raise ValueError("frozen input file exceeds size bound")
    def reject_constant(value):
        raise ValueError("non-finite JSON number: " + value)
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON object field")
            result[key] = value
        return result
    payload = json.loads(raw, parse_constant=reject_constant, object_pairs_hook=unique_pairs)
    scope, signals, observations, as_of, policy = decode_input(payload)
    report = run_capacity_study(scope, signals, observations, as_of=as_of, policy=policy)
    # Do not change report_sha256's pure-study meaning; wrap export metadata.
    output = {
        "schema": "paper_capacity_export_v1", "input_sha256": hashlib.sha256(raw).hexdigest(),
        "input_authentication": "input_bytes_only_upstream_source_references_not_authenticated",
        "database_connected": False, "orders_submitted": False,
        "exported_at": datetime.now().isoformat(), "study": report,
    }
    content = (json.dumps(output, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()
    # Reuse existing atomic, non-overwriting research publication mechanics.
    from app.paper.research_reports import _publish
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".capacity-publish-", dir=destination.parent) as work:
        published = _publish(output, Path(work), datetime.now())
        os.link(published["output"], destination)
    assert destination.read_bytes() == content
    print(json.dumps({"output": str(destination), "sha256": published["sha256"],
                      "signal_count": report["signal_count"], "orders_submitted": False}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
