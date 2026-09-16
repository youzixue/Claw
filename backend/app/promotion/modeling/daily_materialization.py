"""Full EOD hist profile over verified, physical, immutable daily material.

No production callbacks, queries, training, source collection or formula changes.
The default empty source policy cannot publish a ready artifact.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
import math

import numpy as np

from app.core.trade_calendar import is_official_closed_day
from app.promotion.modeling.daily_materials import MaterialArchive, clock, decode, encode, digest
from app.promotion.modeling.feature_coverage import HIST_MATERIALIZATION_VERSION, hist_values_digest
from app.promotion.modeling.features import FEATURE_VERSION
from app.promotion.modeling.historical_dataset import _Bar, _historical_values, _is_main_code, _is_limit_up
from app.promotion.outcome_evidence import formal_outcome_bar_error, _FORMAL_CLOSE_SOURCES

PROFILE = "promotion_eod_hist_full_61_registered_sessions_v1"
READY_SCHEMA = "promotion_eod_hist_ready_v1"
BAR_FIELDS = ("open", "high", "low", "close", "volume", "amount", "turnover", "change_pct", "prev_close")


def _finite(value, *, positive=False):
    return (isinstance(value, (float, int)) and not isinstance(value, bool)
            and math.isfinite(value) and (not positive or value > 0))


def _codes(values):
    if (not isinstance(values, list) or any(not isinstance(v, str) or len(v) != 6 or not all("0" <= c <= "9" for c in v) for v in values)
            or len(set(values)) != len(values)):
        raise ValueError("missing/duplicate/invalid universe")
    return set(values)


def _validated(archive, ref, kind):
    if not isinstance(kind, str) or kind not in {"close", "fund", "pool"}:
        raise ValueError("unsupported_material_kind")
    loaded = archive.read(ref)
    if loaded["status"] != "verified_protocol":
        raise ValueError(",".join(loaded["reasons"]))
    p, manifest = loaded["parsed"], loaded["manifest"]
    if (p.get("source") != manifest["source"] or p.get("source_version") != manifest["source_version"]
            or p.get("kind") != kind or p.get("complete") is not True
            or p.get("finality_verified") is not True or p.get("universe_verified") is not True
            or p.get("expected_from_protocol") is not True):
        raise ValueError("full_market_or_pool_finality_unproven")
    source_at = clock(p.get("source_quote_at"))
    universe_at = clock(p.get("universe_frozen_at"))
    received, observed, archived = (clock(manifest.get(k)) for k in ("received_at", "observed_at", "archive_created_at"))
    if (any(v is None for v in (source_at, universe_at, received, observed, archived))
            or not universe_at <= source_at <= received <= observed <= archived <= archive.now()):
        raise ValueError("source_or_universe_clock_unknown_or_future")
    day = date.fromisoformat(p["trade_date"])
    if source_at.date() != day or source_at.hour < 15:
        raise ValueError("source_not_same_day_terminal_evidence")
    universe = _codes(p.get("universe_codes"))
    expected = _codes(p.get("expected_codes"))
    rows = p.get("rows")
    if not universe or not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("empty_universe_or_missing_rows")
    actual = _codes([r.get("code") for r in rows])
    if actual != expected or not actual <= universe or (kind != "pool" and actual != universe):
        raise ValueError("full_market_or_pool_coverage_missing")
    # Empty event pool is not accepted by the current M0 profile.
    if kind == "pool" and not actual:
        raise ValueError("zero_pool_contract_not_supported")
    if any(r.get("status") != "ok" for r in rows):
        raise ValueError("row_unknown_quarantined_or_suspended")
    if (p.get("price_basis") != "CNY_per_share"
            or p.get("adjustment_basis") != "forward_adjusted"
            or not isinstance(p.get("adjustment_version"), str) or not p["adjustment_version"].strip()
            or p.get("volume_unit") != "shares" or p.get("amount_unit") != "CNY"):
        raise ValueError("price_adjustment_or_unit_contract_unknown")
    if kind == "close" and manifest["source"] not in _FORMAL_CLOSE_SOURCES:
        raise ValueError("close_source_not_formal")
    return loaded


def _window(day, calendar):
    if not isinstance(day, date) or isinstance(day, datetime):
        raise ValueError("trade_date must be date")
    result = []
    cursor = day
    for _ in range(180):
        state = calendar.get(cursor)
        if state is not True and state is not False:
            raise ValueError("registered_calendar_gap")
        if state is True:
            if cursor.weekday() >= 5 or is_official_closed_day(cursor):
                raise ValueError("registered_calendar_conflict")
            result.append(cursor)
        if len(result) == 61:
            if result[0] != day:
                raise ValueError("prediction_day_not_registered")
            return list(reversed(result))
        cursor -= timedelta(days=1)
    raise ValueError("insufficient_61_session_window")


def _compute(archive, *, trade_date, close_refs, fund_refs, pool_ref, calendar):
    days = _window(trade_date, calendar)
    closes = [_validated(archive, ref, "close") for ref in close_refs]
    funds = [_validated(archive, ref, "fund") for ref in fund_refs]
    pool = _validated(archive, pool_ref, "pool")

    def dated(items, required):
        keys = [date.fromisoformat(i["parsed"]["trade_date"]) for i in items]
        if len(set(keys)) != len(keys) or set(keys) != set(required):
            raise ValueError("duplicate_or_missing_material_session")
        return dict(zip(keys, items))

    closes = dated(closes, days)
    funds = dated(funds, days[-3:])
    if pool["parsed"]["trade_date"] != trade_date.isoformat():
        raise ValueError("pool_date_mismatch")
    all_material = list(closes.values()) + list(funds.values()) + [pool]
    universes = {tuple(sorted(m["parsed"]["universe_codes"])) for m in all_material}
    bases = {tuple(m["parsed"][k] for k in ("price_basis", "adjustment_basis", "adjustment_version",
                                           "volume_unit", "amount_unit")) for m in all_material}
    if len(universes) != 1 or len(bases) != 1:
        raise ValueError("universe_or_price_basis_revision_mismatch")
    # Full material universe is retained; old formula's main-board denominator is
    # an explicit static prefix profile, never a current StockTag filter.
    codes = [c for c in next(iter(universes)) if _is_main_code(c)]
    if not codes:
        raise ValueError("empty_formula_universe")
    by_code = {c: [] for c in codes}
    for day in days:
        for row in closes[day]["parsed"]["rows"]:
            if any(not _finite(row.get(n), positive=n in {"open", "high", "low", "close", "volume", "amount", "prev_close"})
                   for n in BAR_FIELDS):
                raise ValueError("missing_or_invalid_close_fields")
            if row["high"] < max(row["open"], row["close"], row["low"]) or row["low"] > min(row["open"], row["close"]):
                raise ValueError("invalid_ohlc")
            if row["code"] not in by_code:
                continue
            values = {name: row[name] for name in BAR_FIELDS}
            if abs(values["change_pct"]) < 1e-9:
                values["change_pct"] = (values["close"] / values["prev_close"] - 1.0) * 100.0
            bar = _Bar(code=row["code"], trade_date=day, **values)
            history = by_code[row["code"]]
            if history:
                from types import SimpleNamespace
                before = SimpleNamespace(close=history[-1].close, volume=history[-1].volume,
                                         source=closes[days[days.index(day) - 1]]["manifest"]["source"])
                after = SimpleNamespace(close=bar.close, volume=bar.volume, prev_close=bar.prev_close,
                                        source=closes[day]["manifest"]["source"])
                if formal_outcome_bar_error(before, after):
                    raise ValueError("formal_price_chain_discontinuity")
            history.append(bar)
    fund_map = {}
    for day, material in funds.items():
        for row in material["parsed"]["rows"]:
            if not all(_finite(row.get(n)) for n in ("main_net_inflow", "main_net_inflow_pct")):
                raise ValueError("missing_fund_group_not_zero")
            fund_map[(row["code"], day)] = (row["main_net_inflow"], row["main_net_inflow_pct"])
    changes = np.asarray([by_code[c][-1].change_pct for c in codes], dtype=float)
    market = {trade_date: {"advance_ratio": float(np.mean(changes > 0)),
                          "limit_up_count": float(sum(_is_limit_up(v) for v in changes)),
                          "median_return": float(np.median(changes)),
                          "return_dispersion": float(np.std(changes))}}
    # Pool is mandatory completeness evidence; hist count remains the original
    # historical formula, NOT silently replaced with vendor event count.
    values_by_code = {c: _historical_values(by_code[c], 60, fund_map=fund_map, market_context=market)[0] for c in codes}
    materialized = archive.now()
    if any(clock(m["manifest"]["archive_created_at"]) > materialized or archive.available_at(m["ref"]) > materialized
           for m in all_material):
        raise ValueError("input_not_published_before_materialization")
    return {"schema_version": READY_SCHEMA, "profile": PROFILE, "feature_version": FEATURE_VERSION,
            "trade_date": trade_date.isoformat(), "policy_id": archive.policy.policy_id,
            "materialized_at": materialized.isoformat(), "values_by_code": values_by_code,
            "input_refs": {"close": close_refs, "fund": fund_refs, "pool": pool_ref},
            "calendar": {d.isoformat(): calendar[d] for d in sorted(calendar) if days[0] <= d <= trade_date},
            "price_contract": list(next(iter(bases))),
            "field_evidence_clock": {
                "source_at": max(clock(m["parsed"]["source_quote_at"]) for m in all_material).isoformat(),
                "received_at": max(clock(m["manifest"]["received_at"]) for m in all_material).isoformat(),
                "observed_at": max(clock(m["manifest"]["observed_at"]) for m in all_material).isoformat()}}


def materialize(archive: MaterialArchive, *, trade_date, close_refs, fund_refs, pool_ref, calendar):
    """Returns ready receipt or immutable blocked diagnostics; never drops names."""
    try:
        if not isinstance(calendar, dict) or any(not isinstance(d, date) or isinstance(d, datetime) for d in calendar):
            raise ValueError("invalid_calendar_structure")
        owned = decode(encode({"close": close_refs, "fund": fund_refs, "pool": pool_ref,
                              "calendar": {d.isoformat(): v for d, v in calendar.items()}}))
        close_refs, fund_refs, pool_ref = owned["close"], owned["fund"], owned["pool"]
        calendar = {date.fromisoformat(d): v for d, v in owned["calendar"].items()}
        if not isinstance(close_refs, list) or not isinstance(fund_refs, list) or not isinstance(pool_ref, dict):
            raise ValueError("invalid_material_ref_groups")
        payload = _compute(archive, trade_date=trade_date, close_refs=close_refs, fund_refs=fund_refs,
                           pool_ref=pool_ref, calendar=calendar)
        return {"status": "ready", "receipt_ref": archive.publish(payload), "profile": PROFILE}
    except (ValueError, KeyError, TypeError, OSError) as exc:
        failure = {"status": "blocked", "profile": PROFILE, "trade_date": str(trade_date),
                   "reason": str(exc), "observed_at": archive.now().isoformat(),
                   "input_refs": {"close": close_refs, "fund": fund_refs, "pool": pool_ref}}
        failure["report_ref"] = archive.put(encode(failure))
        return failure


@dataclass(frozen=True)
class LockedReady:
    # Immutable serialized payload prevents caller mutation and revision reselection.
    payload: bytes
    receipt: bytes
    cutoff: datetime


def lock_ready(archive: MaterialArchive, *, trade_date, cutoff):
    """Read-only export at cutoff <= actual now; corruption never falls back.

    Older trade dates may be exported, but bind_candidate refuses to produce a
    prediction proof for them. Call from a worker thread, not the async request
    event loop: physical verification parses the complete 61-session universe.
    """
    if not isinstance(cutoff, datetime) or clock(cutoff) is None:
        raise ValueError("cutoff must be naive datetime")
    if cutoff > archive.now():
        raise ValueError("cutoff_after_actual_now")
    if not isinstance(trade_date, date) or isinstance(trade_date, datetime):
        raise ValueError("trade_date must be date")
    if trade_date > cutoff.date():
        raise ValueError("trade_date_after_cutoff")
    choices = []
    directory = archive._safe(archive.root / "ready")
    for path in sorted(directory.glob("*.blob")):
        ref = {"path": f"ready/{path.name}", "sha256": path.stem, "size": path.stat().st_size}
        raw = archive.read_bytes(ref)
        receipt = decode(raw)
        if not isinstance(receipt, dict) or receipt.get("schema_version") != "promotion_daily_ready_receipt_v1":
            raise ValueError("invalid ready receipt")
        published = clock(receipt.get("published_at"))
        if published is None:
            raise ValueError("missing publication clock")
        if max(published, archive.available_at(ref)) > cutoff:
            continue
        payload_raw = archive.read_bytes(receipt["ready_ref"])
        payload = decode(payload_raw)
        if not isinstance(payload, dict):
            raise ValueError("invalid_ready_payload_structure")
        if payload.get("trade_date") == trade_date.isoformat():
            choices.append((published, ref["sha256"], payload_raw, raw))
    if not choices:
        return None
    newest = max(item[0] for item in choices)
    latest = [item for item in choices if item[0] == newest]
    if len(latest) != 1:
        raise ValueError("ambiguous_same_clock_ready_revisions")
    _, _, payload_raw, receipt_raw = latest[0]
    receipt = decode(receipt_raw)
    if (receipt.get("policy_id") != archive.policy.policy_id
            or archive.available_at(receipt["ready_ref"]) > clock(receipt["published_at"])):
        raise ValueError("ready_not_available_at_declared_publication")
    payload = decode(payload_raw)
    if (payload.get("profile") != PROFILE or payload.get("schema_version") != READY_SCHEMA
            or payload.get("feature_version") != FEATURE_VERSION
            or payload.get("policy_id") != archive.policy.policy_id):
        raise ValueError("ready profile or reviewed policy mismatch")
    materialized = clock(payload.get("materialized_at"))
    if materialized is None or materialized > clock(receipt["published_at"]) or materialized > cutoff:
        raise ValueError("materialization_after_publication_or_cutoff")
    inputs = payload["input_refs"]
    for ref in [*inputs["close"], *inputs["fund"], inputs["pool"]]:
        material = archive.read(ref)
        manifest = material["manifest"]
        if (clock(manifest.get("archive_created_at")) is None
                or clock(manifest["archive_created_at"]) > materialized
                or archive.available_at(ref) > materialized
                or archive.available_at(manifest["raw_ref"]) > materialized):
            raise ValueError("input_published_after_materialization")
    # Verify every physical original again at lock time, plus deterministic values.
    replay = _compute(archive, trade_date=trade_date, close_refs=inputs["close"],
                      fund_refs=inputs["fund"], pool_ref=inputs["pool"],
                      calendar={date.fromisoformat(d): v for d, v in payload["calendar"].items()})
    if replay["values_by_code"] != payload["values_by_code"]:
        raise ValueError("ready_values_do_not_match_physical_material")
    for name in ("field_evidence_clock", "price_contract", "input_refs", "calendar"):
        if replay[name] != payload[name]:
            raise ValueError("ready_evidence_mismatch")
    return LockedReady(payload_raw, receipt_raw, cutoff)


def bind_candidate(locked: LockedReady | None, *, code):
    """Bind same-day prediction evidence only; old-day values are export-only.

    Never compute/read new revisions or alter a run. The parent integration
    additionally owns the promotion_2000 schedule-context gate.
    """
    return bind_candidates(locked, codes=(code,))[code]


def bind_candidates(locked: LockedReady | None, *, codes):
    """Bind one immutable revision in a single decode, not universe × candidates."""
    codes = tuple(codes)
    if any(not isinstance(code, str) or len(code) != 6 or not all("0" <= c <= "9" for c in code) for code in codes):
        raise ValueError("candidate code must be six ASCII digits")
    if locked is None:
        return {code: {"status": "blocked", "reason": "no_ready_before_cutoff"} for code in codes}
    p, receipt = decode(locked.payload), decode(locked.receipt)
    return {code: _bind_candidate_payload(locked, p, receipt, code) for code in codes}


def _bind_candidate_payload(locked, p, receipt, code):
    values = p["values_by_code"].get(code)
    if values is None:
        return {"status": "blocked", "reason": "candidate_missing_from_full_profile"}
    if p["trade_date"] != locked.cutoff.date().isoformat():
        return {"status": "retrospective_export_only", "values": values,
                "reason": "material_day_differs_from_prediction_cutoff", "forward_eligible": False}
    evidence = {**p["field_evidence_clock"], "status": "ok", "source": "reviewed_daily_material_bundle",
                "source_version": p["policy_id"], "source_manifest_sha256": receipt["ready_ref"]["sha256"]}
    proof = {"schema_version": HIST_MATERIALIZATION_VERSION, "feature_version": FEATURE_VERSION,
             "code": code, "prediction_trade_date": p["trade_date"],
             "as_of_at": locked.cutoff.isoformat(), "materialized_at": p["materialized_at"],
             "values_sha256": hist_values_digest(values),
             "fields": {name: dict(evidence) for name in values},
             "material_ref": receipt["ready_ref"], "profile": PROFILE}
    return {"status": "ready", "values": values, "hist_materialization": proof}
