"""Owned read-time factor captures and exact-version isolated recomputation.

No historical PIT eligibility, trade authority, live-source fallback or automatic
selection. Bytecode fingerprints describe LOADED callables (never current disk
source pretending to be an old process); no stored bytecode is executed.
"""
from datetime import date, datetime, time
import hashlib
import json
import marshal
import platform

import numpy as np
import pandas as pd
from sqlalchemy import select

from app.factors import base
from app.factors.market_inputs import MARKET_FIELDS, FUND_FIELDS, WINDOW, PROTOCOL as INPUT_PROTOCOL, validate_code
from app.models.factor import FactorComputationRun

PROTOCOL = "factor_computation_capture_v1"
FIELDS = ("trade_date", *MARKET_FIELDS, *FUND_FIELDS)


def freeze(value):
    def default(leaf):
        if isinstance(leaf, (date, datetime)):
            return leaf.isoformat()
        raise TypeError("factor capture requires owned JSON leaves")
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False, default=default)


def digest(value):
    return hashlib.sha256(freeze(value).encode()).hexdigest()


def _code_hash(function):
    code = getattr(function, "__code__", None)
    if code is None:
        raise ValueError("factor implementation is not a loaded Python callable")
    return hashlib.sha256(marshal.dumps(code)).hexdigest()


def engine_descriptor(engine):
    """Audit/replay compatibility token, not a signed build or full environment lock."""
    factors = {}
    for name, factor in engine.registry.all_factors().items():
        calculate = factor.calculate
        original = getattr(calculate, "__wrapped__", calculate)
        factors[name] = {
            "name": factor.factor_name, "category": factor.category.value,
            "direction": factor.direction, "dependencies": factor.dependencies,
            "input_window": factor.input_window, "required_context": factor.required_context,
            "conditional_context": factor.conditional_context,
            "context_enums": factor.context_enums, "context_bounds": factor.context_bounds,
            "input_contract": getattr(calculate, "input_contract", None),
            "calculator_sha256": _code_hash(original), "validator_sha256": _code_hash(calculate),
        }
    result = {
        "python": platform.python_version(), "numpy": np.__version__, "pandas": pd.__version__,
        "factors": factors,
        "helpers": {key: _code_hash(function) for key, function in {
            "compute_single": engine.compute_single, "finite_number": base._finite_number,
            "finite_meta": base._finite_meta, "result_contract": base.FactorResult.__post_init__,
            "safe_value": base.FactorBase._safe_value, "unavailable": base.FactorBase._unavailable,
        }.items()},
        "basis": "loaded_callable_bytecode_and_declared_contract_not_signed_build",
    }
    return json.loads(freeze(result))  # Own mutable class containers before an await.


def frame_material(frame):
    if (not isinstance(frame, pd.DataFrame) or tuple(frame.columns) != FIELDS
            or len(frame) != WINDOW):
        raise ValueError("factor frame schema/window mismatch")
    rows = []
    for raw in frame.to_dict(orient="records"):
        day = raw["trade_date"]
        if type(day) is not date:
            raise ValueError("factor frame date invalid")
        row = {"trade_date": day.isoformat()}
        for key in FIELDS[1:]:
            value = raw[key]
            number = base._finite_number(value)
            if number is None and value is not None and not (
                    isinstance(value, (float, np.floating)) and np.isnan(value)):
                raise ValueError("factor frame contains unsupported numeric leaf")
            row[key] = number
        rows.append(row)
    days = [row["trade_date"] for row in rows]
    if days != sorted(set(days)):
        raise ValueError("factor frame dates must be ordered distinct sessions")
    return rows


def result_material(results, descriptor):
    if set(results) != set(descriptor["factors"]):
        raise ValueError("factor result denominator differs from loaded registry")
    rows = {}
    for name, value in results.items():
        if not isinstance(value, base.FactorResult) or value.factor_name != name:
            raise ValueError("factor result identity mismatch")
        rows[name] = {
            "value": value.value, "rank": value.rank, "pct": value.pct,
            "direction": value.direction, "confidence": value.confidence, "meta": value.meta,
        }
    return json.loads(freeze(rows))


def freeze_stock(*, code, trade_date, read_started_at, material, evidence, results, descriptor):
    """Freeze immediately after computation, before another stock or DB await."""
    validate_code(code)
    if (evidence.get("protocol") != INPUT_PROTOCOL or evidence.get("code") != code
            or evidence.get("trade_date") != trade_date.isoformat()
            or evidence.get("as_of_at") != read_started_at.isoformat()
            or evidence.get("session_dates") != [row["trade_date"] for row in material]
            or evidence.get("input_values_sha256") != digest(material)
            or evidence.get("point_in_time_verified") is not False):
        raise ValueError("factor frame/evidence identity, cutoff or hash mismatch")
    return json.loads(freeze({
        "frame": material, "context": {}, "market_evidence": evidence,
        "results": result_material(results, descriptor),
    }))


def validate_payload(payload):
    """Validate an owned capture envelope, without upgrading it to PIT authority."""
    try:
        if (type(payload) is not dict or payload["protocol"] != PROTOCOL
                or any(payload[key] is not False for key in (
                    "point_in_time_verified", "trading_authority", "promotion_eligible", "automatic_weight_update"))
                or payload["physical_commit_at"] is not None
                or payload["clock_basis"] != "local_capture_before_commit"
                or payload["scope"] != "bounded_per_stock_read_time_daily_research_not_ranked_cross_section"):
            raise ValueError("factor capture authority/protocol mismatch")
        started, captured = (datetime.fromisoformat(payload[key])
                             for key in ("read_started_at", "captured_at"))
        day = date.fromisoformat(payload["trade_date"])
        if (started.tzinfo is not None or captured.tzinfo is not None or started > captured
                or day > started.date() or (day == started.date() and started.time() < time(15, 10))
                or not isinstance(payload["capture_id"], str)
                or not payload["capture_id"].strip() or len(payload["capture_id"]) > 40):
            raise ValueError("factor capture clock/identity mismatch")
        requested, attempted, deferred = (payload[key] for key in (
            "requested_codes", "attempted_codes", "deferred_codes"))
        if (any(type(codes) is not list or codes != sorted(set(codes))
                for codes in (requested, attempted, deferred))
                or attempted != requested[:100] or deferred != requested[100:]):
            raise ValueError("factor capture bounded denominator mismatch")
        for code in requested:
            validate_code(code)
        stocks, failures = payload["stocks"], payload["failures"]
        if (type(stocks) is not dict or type(failures) is not dict
                or set(stocks) & set(failures) or set(stocks) | set(failures) != set(attempted)
                or any(not isinstance(value, str) or not value for value in failures.values())):
            raise ValueError("factor capture result partition mismatch")
        descriptor = payload["descriptor"]
        if (payload["implementation_hash"] != digest(descriptor)
                or not isinstance(descriptor["factors"], dict) or not descriptor["factors"]):
            raise ValueError("factor implementation descriptor mismatch")
        for code, stock in stocks.items():
            material, evidence = stock["frame"], stock["market_evidence"]
            if (type(material) is not list or len(material) != WINDOW
                    or evidence["protocol"] != INPUT_PROTOCOL or evidence["code"] != code
                    or evidence["trade_date"] != day.isoformat()
                    or evidence["as_of_at"] != started.isoformat()
                    or evidence["point_in_time_verified"] is not False
                    or stock["context"] != {} or set(stock["results"]) != set(descriptor["factors"])):
                raise ValueError("factor stock contract mismatch")
            dates = [date.fromisoformat(row["trade_date"]) for row in material]
            if (dates != sorted(set(dates)) or dates[-1] != day
                    or [d.isoformat() for d in dates] != evidence["session_dates"]
                    or digest(material) != evidence["input_values_sha256"]
                    or any(set(row) != set(FIELDS) or any(
                        row[key] is not None and base._finite_number(row[key]) is None
                        for key in FIELDS[1:]) for row in material)):
                raise ValueError("factor capture material mismatch")
            for name, result in stock["results"].items():
                confidence = base._finite_number(result["confidence"])
                value = result["value"]
                if (type(result["meta"]) is not dict or confidence is None or not 0 <= confidence <= 1
                        or result["direction"] != descriptor["factors"][name]["direction"]
                        or result["rank"] is not None or result["pct"] is not None
                        or (value is None and confidence != 0)
                        or (value is not None and base._finite_number(value) is None)):
                    raise ValueError("factor capture result contract mismatch")
        freeze(payload)  # Strict JSON, including metadata and descriptor leaves.
    except (KeyError, TypeError, OverflowError) as exc:
        raise ValueError("malformed factor computation capture") from exc


async def append_computation(db, *, capture_id, trade_date, read_started_at, captured_at,
                             descriptor, stocks, requested_codes, attempted_codes, deferred_codes, failures):
    """Caller owns transaction and COMMIT; serialized full denominator before first await."""
    clocks = (read_started_at, captured_at)
    if (not all(isinstance(at, datetime) and at.tzinfo is None for at in clocks)
            or read_started_at > captured_at or type(trade_date) is not date
            or trade_date > read_started_at.date()
            or not isinstance(capture_id, str) or not 1 <= len(capture_id) <= 40):
        raise ValueError("invalid factor computation identity/real capture clock")
    for codes in (requested_codes, attempted_codes, deferred_codes):
        if type(codes) is not list or codes != sorted(set(codes)):
            raise ValueError("factor capture denominator must be sorted distinct codes")
        for code in codes:
            validate_code(code)
    if (requested_codes != sorted(attempted_codes + deferred_codes)
            or set(attempted_codes) & set(deferred_codes)
            or set(stocks) & set(failures) or set(stocks) | set(failures) != set(attempted_codes)):
        raise ValueError("factor capture denominator partition mismatch")
    for code, stock in stocks.items():
        evidence = stock["market_evidence"]
        if (evidence["code"] != code or evidence["trade_date"] != trade_date.isoformat()
                or evidence["as_of_at"] != read_started_at.isoformat()
                or digest(stock["frame"]) != evidence["input_values_sha256"]
                or set(stock["results"]) != set(descriptor["factors"])
                or stock["context"] != {}):
            raise ValueError("factor stock capture mismatch")
    body = freeze({
        "protocol": PROTOCOL, "capture_id": capture_id, "trade_date": trade_date.isoformat(),
        "read_started_at": read_started_at.isoformat(), "captured_at": captured_at.isoformat(),
        "clock_basis": "local_capture_before_commit", "physical_commit_at": None,
        "point_in_time_verified": False, "trading_authority": False,
        "promotion_eligible": False, "automatic_weight_update": False,
        "scope": "bounded_per_stock_read_time_daily_research_not_ranked_cross_section",
        "descriptor": descriptor, "implementation_hash": digest(descriptor), "stocks": stocks,
        "requested_codes": requested_codes, "attempted_codes": attempted_codes,
        "deferred_codes": deferred_codes, "failures": failures,
    })
    validate_payload(json.loads(body))
    sha = hashlib.sha256(body.encode()).hexdigest()
    with db.no_autoflush:
        previous = (await db.execute(select(
            FactorComputationRun.id, FactorComputationRun.payload_hash, FactorComputationRun.payload_json,
        ).where(FactorComputationRun.capture_id == capture_id))).one_or_none()
    if previous is not None:
        if previous.payload_hash != sha or previous.payload_json != body:
            raise ValueError("immutable factor computation collision")
        run_id, inserted = previous.id, False
    else:
        run = FactorComputationRun(capture_id=capture_id, trade_date=trade_date,
            read_started_at=read_started_at, captured_at=captured_at, protocol_version=PROTOCOL,
            payload_hash=sha, payload_json=body)
        db.add(run)
        await db.flush()
        run_id, inserted = run.id, True
    return {"status": "flushed_not_committed", "run_id": run_id, "capture_id": capture_id,
            "payload_hash": sha, "payload_bytes": len(body.encode()), "inserted": inserted,
            "point_in_time_verified": False}


async def read_computation(db, *, capture_id):
    """Read one explicit immutable identity, never infer 'latest' at an old decision."""
    row = (await db.execute(select(
        FactorComputationRun.capture_id, FactorComputationRun.trade_date,
        FactorComputationRun.read_started_at, FactorComputationRun.captured_at,
        FactorComputationRun.protocol_version, FactorComputationRun.payload_hash,
        FactorComputationRun.payload_json,
    ).where(FactorComputationRun.capture_id == capture_id)
      .execution_options(autoflush=False))).one_or_none()
    if row is None:
        return None
    if hashlib.sha256(row.payload_json.encode()).hexdigest() != row.payload_hash:
        raise ValueError("factor computation payload corrupt")
    payload = json.loads(row.payload_json)
    if (payload["protocol"] != PROTOCOL or row.protocol_version != PROTOCOL
            or payload["capture_id"] != row.capture_id
            or payload["trade_date"] != row.trade_date.isoformat()
            or payload["read_started_at"] != row.read_started_at.isoformat()
            or payload["captured_at"] != row.captured_at.isoformat()
            or payload["implementation_hash"] != digest(payload["descriptor"])
            or payload["point_in_time_verified"] is not False):
        raise ValueError("factor computation record contract mismatch")
    validate_payload(payload)
    return payload


async def replay_computation(payload, engine):
    """Recompute ONLY the frozen calculation under an identical loaded descriptor.

    No live source lookup and no historical reconstruction; caller can compare a
    saved calculation even after mutable daily tables change. Version mismatch
    is an explicit error, never a silent replay under today's new formulas.
    """
    validate_payload(payload)
    payload = json.loads(freeze(payload))
    current = engine_descriptor(engine)
    if (payload.get("protocol") != PROTOCOL or payload.get("point_in_time_verified") is not False
            or payload.get("implementation_hash") != digest(current)
            or payload.get("descriptor") != current):
        raise ValueError("factor replay implementation/protocol mismatch")
    matches = {}
    for code, stock in payload["stocks"].items():
        if digest(stock["frame"]) != stock["market_evidence"]["input_values_sha256"] or stock["context"] != {}:
            raise ValueError("factor replay frozen input mismatch")
        rows = [{**row, "trade_date": date.fromisoformat(row["trade_date"])} for row in stock["frame"]]
        frame = pd.DataFrame(rows, columns=FIELDS)
        if frame_material(frame) != stock["frame"]:
            raise ValueError("factor replay frame mismatch")
        result = await engine.compute_single(code, date.fromisoformat(payload["trade_date"]), frame)
        if engine_descriptor(engine) != current or frame_material(frame) != stock["frame"]:
            raise ValueError("factor replay implementation/input changed during calculation")
        matches[code] = result_material(result, current) == stock["results"]
    return {"protocol": PROTOCOL, "matched": all(matches.values()) if matches else None,
            "evaluated_stock_count": len(matches), "stocks": matches,
            "scope": "calculation_only_not_outcome_or_pit_verification", "point_in_time_verified": False}
