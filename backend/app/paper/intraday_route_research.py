"""Frozen intraday confirmation comparisons. No DB, execution, settings mutation or IO.

This is a confirmation-gate ablation on supplied contemporaneous setup evidence,
NOT a replay of full stock selection/risk rules. Source prices are observations,
never assumed fills. Existing route/account rules are explicitly snapshotted only
by freeze_current_route_context; historical evaluation never reads live settings.
"""
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta
import hashlib
import json
import math
import re

from app.paper.account_policy import ROUTE_ACCOUNT_NAMES

from app.paper.strategy_iteration_shadow import ROUTE_B, ROUTE_C, ROUTE_C3, ROUTE_F2, _pct
from app.paper.momentum_retest_shadow import ROUTE_ID as ROUTE_A2

ROUTE_E2 = "e2_highboard_reseal"
SUPPORTED_ROUTES = (ROUTE_B, ROUTE_C, ROUTE_C3, ROUTE_F2, ROUTE_A2, ROUTE_E2)
ROUTE_ACCOUNTS = {route: ROUTE_ACCOUNT_NAMES[route] for route in (ROUTE_B, ROUTE_C, ROUTE_F2, ROUTE_A2)}
ROUTE_ACCOUNTS[ROUTE_E2] = "challenger_e"
# C3 is evidence-only: never invent a trading account or a position for research.
ROUTE_ACCOUNTS[ROUTE_C3] = None
ENTRY_SHAPE_FAMILIES = ("repair_first_board", "first_retest", "second_ignition")
HORIZONS = (1, 3, 5, 10)


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return float(value) if math.isfinite(value) else None
    except (ValueError, OverflowError):
        return None


def _clock(value):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    return value if isinstance(value, datetime) and value.tzinfo is None else None


def _owned(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _owned(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_owned(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return {"invalid_number": "nan" if math.isnan(value) else "+inf" if value > 0 else "-inf"}
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    return {"invalid_type": type(value).__name__}  # Never inspect/stringify unknown or ORM objects.


def _hash(value):
    return hashlib.sha256(json.dumps(_owned(value), sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _session(at):
    if time(9, 30) <= at.time() <= time(11, 30):
        return "morning"
    if time(13) <= at.time() <= time(15):
        return "afternoon"
    return None


def _seconds(at):
    morning = datetime.combine(at.date(), time(9, 30))
    afternoon = datetime.combine(at.date(), time(13))
    return min(max((at-morning).total_seconds(), 0), 7200) + min(max((at-afternoon).total_seconds(), 0), 7200)


def _horizon(at, minutes):
    target = _seconds(at) + minutes*60
    if target > 14400:
        return None
    if target <= 7200:
        return datetime.combine(at.date(), time(9, 30)) + timedelta(seconds=target)
    return datetime.combine(at.date(), time(13)) + timedelta(seconds=target-7200)


@dataclass(frozen=True)
class RouteResearchPolicy:
    version: str
    min_samples: int
    min_persistence_sec: int
    max_sample_gap_sec: int
    max_source_age_sec: int = 180
    clock_jitter_sec: float = 0
    endpoint_tolerance_sec: int = 30

    def __post_init__(self):
        if not isinstance(self.version, str) or not self.version.startswith("research:"):
            raise ValueError("independent research: policy version required")
        for name in ("min_samples", "max_sample_gap_sec", "max_source_age_sec"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError("positive integer research parameter required")
        for name in ("min_persistence_sec", "endpoint_tolerance_sec"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError("nonnegative integer research parameter required")
        if _number(self.clock_jitter_sec) is None or self.clock_jitter_sec < 0:
            raise ValueError("invalid research clock jitter")

    def contract(self):
        return {**asdict(self), "policy_hash": _hash(asdict(self)),
                "confirmation_basis": "distinct_source_frames_wall_clock_no_lunch_bridge",
                "horizon_basis": "same_day_trading_minutes",
                "horizons_minutes": list(HORIZONS),
                "endpoint_basis": "last_source_at_or_before_target_with_explicit_tolerance",
                "comparison_scope": "frozen_setup_confirmation_gate_only"}


def freeze_current_route_context(route_id):
    """Explicit NOW-only adapter; never attach today's settings to old candidates."""
    from app.paper.account_policy import account_parameter_snapshot, ROUTE_ACCOUNT_NAMES
    from app.paper.strategy_iteration_shadow import _rules, route_version_for
    from app.paper.momentum_retest_shadow import MomentumRetestPolicy
    if route_id not in SUPPORTED_ROUTES:
        raise ValueError("unsupported route")
    account = ROUTE_ACCOUNTS[route_id]
    momentum = MomentumRetestPolicy.from_settings().snapshot()
    result = {"account_policy": account_parameter_snapshot(account) if account else None,
              "account_name": account, "evidence_only": route_id == ROUTE_C3,
              "availability": "caller_must_record_actual_freeze_time_no_historical_backfill",
              "momentum_path_reference": momentum}
    if route_id == ROUTE_A2:
        result.update(route_rules=momentum, production_version=momentum["version"])
    elif route_id != ROUTE_E2:
        result.update(route_rules=_rules(route_id), production_version=route_version_for(route_id))
    # E2 version must come from its real frozen candidate, never guessed here.
    return _owned(result)


def _ref(value):
    return isinstance(value, str) and bool(value.strip())


def _same_account(row, candidate):
    identity_ok = (type(row.get("account_id")) is int
                   and row["account_id"] == candidate["account_id"])
    if candidate.get("route_id") == ROUTE_C3:
        identity_ok = row.get("account_id") is None and candidate.get("account_id") is None
    return (identity_ok and row.get("account_name") == candidate["account_name"]
            and row.get("production_version") == candidate["production_version"])


def _rejected_evidence(raw, reason):
    # Only selected scalar leaves; never recursively enumerate arbitrary supplied objects.
    keys = ("candidate_id", "sample_id", "account_id", "account_name", "code",
            "production_version", "evidence_ref", "predicate_ref", "source_quote_at",
            "received_at", "observed_at", "price", "prev_close", "price_basis",
            "setup_valid", "candidate_active", "original_trigger", "limit_up", "limit_evidence_ref",
            "shape_stage", "shape_event_ref")
    leaves = {}
    if type(raw) is dict:
        for key in keys:
            if key not in raw:
                leaves[key] = {"type": "missing"}
                continue
            value = raw[key]
            scalar = value is None or type(value) in (str, int, float, bool, datetime)
            leaves[key] = {"type": type(value).__name__,
                           "value": _owned(value) if scalar else {"invalid_type": type(value).__name__}}
    else:
        leaves["input"] = {"invalid_type": type(raw).__name__}
    return {"reason": reason, "raw_leaf_hash": _hash(leaves),
            "identity_and_clock": {k: leaves[k] for k in (
                "candidate_id", "sample_id", "account_id", "account_name", "evidence_ref",
                "source_quote_at", "received_at", "observed_at") if k in leaves}}


def _candidate_error(c, as_of):
    if not isinstance(c, dict):
        return "candidate_not_object"
    if not all(_ref(c.get(k)) for k in
               ("candidate_id", "code", "production_version", "evidence_ref", "price_basis")):
        return "candidate_identity_missing"
    if not re.fullmatch(r"[0-9]{6}", c["code"]):
        return "code_not_ascii_six_digits"
    if c.get("route_id") not in SUPPORTED_ROUTES:
        return "route_unsupported"
    if c["route_id"] == ROUTE_C3:
        if (not all(k in c for k in ("account_id", "account_name"))
                or c.get("account_id") is not None or c.get("account_name") is not None
                or c.get("evidence_only") is not True):
            return "evidence_only_route_cannot_claim_execution_account"
    elif (type(c.get("account_id")) is not int or c["account_id"] <= 0
            or c.get("account_name") != ROUTE_ACCOUNTS[c["route_id"]]):
        return "candidate_account_route_unproven"
    frozen = _clock(c.get("frozen_at"))
    if frozen is None or frozen > as_of or not _session(frozen):
        return "candidate_clock_invalid_or_future"
    if c.get("trade_date") != frozen.date().isoformat() or c.get("is_trade_day") is not True:
        return "trade_calendar_unproven"
    if not _ref(c.get("calendar_ref")):
        return "calendar_evidence_missing"
    if c.get("eligible") is not True:
        return "candidate_ineligible_or_unknown"
    if not isinstance(c.get("frozen_account_policy"), dict) or not c["frozen_account_policy"]:
        return "frozen_account_policy_missing"
    if not _same_account(c["frozen_account_policy"], c):
        return "frozen_policy_account_version_conflict"
    if not isinstance(c.get("frozen_position"), dict):
        return "frozen_position_missing"
    if not _same_account(c["frozen_position"], c):
        return "frozen_position_account_version_conflict"
    if (c["route_id"] == ROUTE_C3
            and c["frozen_position"].get("position_basis") != "not_applicable_evidence_only"):
        return "evidence_only_position_must_be_explicitly_not_applicable"
    if c["route_id"] == ROUTE_C3 and c.get("actual_fills"):
        return "evidence_only_route_cannot_claim_actual_fills"
    if any(_number(c.get(k)) is None or c[k] <= 0 for k in ("prev_close", "trigger_price")):
        return "candidate_price_basis_invalid"
    return ""


def _prepare(c, rows, as_of, policy):
    """Order by actual observation, not by later-reconstructed source sequence."""
    valid, rejects, seen_ids, seen_sources = [], Counter(), {}, {}
    fatal = False
    rejected_evidence = []
    def reject(raw, reason):
        rejects[reason] += 1
        rejected_evidence.append(_rejected_evidence(raw, reason))
    for raw in rows:
        if not isinstance(raw, dict):
            reject(raw, "sample_not_object")
            fatal = True
            continue
        observed = _clock(raw.get("observed_at"))
        if observed is not None and observed > as_of:
            reject(raw, "future_observation_excluded")
            continue
        source, received = _clock(raw.get("source_quote_at")), _clock(raw.get("received_at"))
        reason = ""
        if (source is None or received is None or observed is None
                or not source <= received <= observed <= as_of
                or any(t.date() != _clock(c["frozen_at"]).date() for t in (source, received, observed))
                or source < _clock(c["frozen_at"]) or not _session(source)
                or _session(observed) != _session(source) or not _session(received)
                or _session(received) != _session(source)
                or (observed-source).total_seconds() > policy.max_source_age_sec):
            reason = "sample_clock_invalid"
        if (not _ref(raw.get("sample_id"))
                or raw.get("code") != c["code"]
                or not _same_account(raw, c)
                or not _ref(raw.get("evidence_ref"))):
            reason = "sample_identity_unproven"
        if (raw.get("price_basis") != c["price_basis"] or _number(raw.get("prev_close")) != c["prev_close"]
                or _number(raw.get("price")) is None or raw["price"] <= 0):
            reason = "sample_price_basis_invalid"
        if reason:
            reject(raw, reason)
            # Cannot reliably locate an unknown frame without inventing a path.
            fatal = True
            continue
        item = {k: raw.get(k) for k in ("sample_id", "code", "account_id", "account_name", "production_version", "evidence_ref",
            "price", "prev_close", "price_basis", "setup_valid", "candidate_active",
            "original_trigger", "predicate_ref", "limit_up", "limit_evidence_ref",
            "shape_stage", "shape_event_ref")}
        # Optional research leaves participate in duplicate/source fingerprints.
        # Never reinterpret production setup_valid as the independent launch gate.
        for key in ("launch_setup_valid", "launch_predicate_ref", "launch_version", "launch_policy_ref",
                    "book_source_at", "book_evidence_ref", "ask1_price",
                    "ask1_volume", "bid1_price", "bid1_volume"):
            if key in raw:
                item[key] = raw[key]
        if not _ref(item["predicate_ref"]):
            reject(raw, "predicate_reference_unproven")
            item["predicate_ref"] = None
        if not _ref(item["limit_evidence_ref"]):
            if c["route_id"] == ROUTE_E2:
                reject(raw, "limit_reference_unproven")
            item["limit_evidence_ref"] = None
        item.update(source_quote_at=source, received_at=received, observed_at=observed)
        # Ignore daily high/low: neither enters temporal evidence or markout.
        fingerprint = _hash({k: v for k, v in item.items() if k not in
                             {"sample_id", "received_at", "observed_at", "evidence_ref"}})
        conflict = False
        for registry, key in ((seen_ids, item["sample_id"]), (seen_sources, source)):
            if key in registry and registry[key] != fingerprint:
                conflict = True
            registry[key] = fingerprint
        if conflict:
            reject(raw, "conflicting_duplicate")
            fatal = True
        valid.append(item)
    valid.sort(key=lambda r: (r["observed_at"], r["source_quote_at"], r["sample_id"]))
    ordered, sources = [], set()
    last = None
    for row in valid:
        source = row["source_quote_at"]
        if source in sources:
            reject(row, "duplicate_source_deduplicated")
            continue
        if last is not None and source < last:
            reject(row, "source_arrival_order_conflict")
            fatal = True
        ordered.append(row)
        sources.add(source)
        last = source
    return ordered, dict(rejects), fatal, sorted(rejected_evidence, key=lambda r: (r["reason"], r["raw_leaf_hash"]))


def _event(row, kind):
    return {"kind": kind, "sample_id": row["sample_id"], "source_quote_at": row["source_quote_at"],
            "observed_at": row["observed_at"], "reference_price": row["price"],
            "evidence_ref": row["evidence_ref"], "reference_is_fill": False}


def _markouts(event, rows, as_of, policy):
    results = {}
    start = event["source_quote_at"]
    # Start after the signal was actually observable, not stale source time.
    anchor = max(start, event["observed_at"])
    for minutes in HORIZONS:
        end = _horizon(anchor, minutes)
        result = {"status": "unknown", "reason": "", "target_at": end,
                  "sampled_mae_pct": None, "sampled_mfe_pct": None,
                  "endpoint_markout_pct": None, "endpoint_at": None,
                  "sample_count": 0, "executable_return": None, "net_profit": None}
        points = [r for r in rows if r["source_quote_at"] > anchor
                  and end is not None and r["source_quote_at"] <= min(end, as_of)]
        result["sample_count"] = len(points)
        if points:
            changes = [_pct(r["price"], event["reference_price"]) for r in points]
            result.update(sampled_mae_pct=round(min(0, *changes), 6),
                          sampled_mfe_pct=round(max(0, *changes), 6))
        if end is None or as_of < end:
            result["reason"] = "right_censored"
        elif not points:
            result["reason"] = "samples_missing"
        else:
            bounds = [anchor, *[r["source_quote_at"] for r in points], end]
            max_gap = max(_seconds(b)-_seconds(a) for a, b in zip(bounds, bounds[1:]))
            result["max_trading_gap_sec"] = max_gap
            last = points[-1]
            if max_gap > policy.max_sample_gap_sec:
                result["reason"] = "coverage_gap"
            elif _seconds(end)-_seconds(last["source_quote_at"]) > policy.endpoint_tolerance_sec:
                result["reason"] = "endpoint_missing"
            else:
                result.update(status="observed", reason="sampled_not_tick_complete",
                    endpoint_markout_pct=round(_pct(last["price"], event["reference_price"]), 6),
                    endpoint_at=last["source_quote_at"])
        results[str(minutes)] = result
    return results


def _compare(c, rows, as_of, policy, blocked):
    names = ("original", "sustained", "recross", "reseal")
    out = {name: {"status": "unknown", "status_basis": "historical_observed_event_not_current_permission",
                  "current_status": "unknown", "first_event": None, "events": [], "markouts": {}}
           for name in names}
    # A broken challenger path must never erase the independently frozen original trigger.
    out["original"]["observations"] = [{"sample_id": row["sample_id"],
        "observed_at": row["observed_at"], "predicate_ref": row["predicate_ref"],
        "status": "true" if row["original_trigger"] is True and row["predicate_ref"] else
                  "false" if row["original_trigger"] is False and row["predicate_ref"] else "unknown"}
        for row in rows]
    for row in rows:
        if row["original_trigger"] is True and row["predicate_ref"]:
            out["original"]["events"].append(_event(row, "frozen_original_trigger"))
    if out["original"]["events"]:
        out["original"]["status"] = "true"
        out["original"]["first_event"] = out["original"]["events"][0]
    if blocked:
        out["original"]["reason"] = "comparison_path_unproven_original_evidence_retained"
        return out
    streak, previous = [], None
    sustained_ready = False
    seen_above = False
    fell_after_above = False
    e2_open_seen = False
    terminated = False
    for row in rows:
        if terminated:
            continue
        if row["candidate_active"] is False:
            terminated = True
            continue
        gap = previous is not None and (
            (row["source_quote_at"]-previous["source_quote_at"]).total_seconds() > policy.max_sample_gap_sec
            or _session(row["source_quote_at"]) != _session(previous["source_quote_at"]))
        if gap or row["candidate_active"] is not True:
            streak, seen_above, fell_after_above, e2_open_seen = [], False, False, False
            previous = None
            sustained_ready = False
        if row["candidate_active"] is not True:
            continue
        predicate = row["setup_valid"] if row["predicate_ref"] else None
        above = row["price"] >= c["trigger_price"]
        if predicate is True and above:
            streak.append(row)
            duration = (row["source_quote_at"]-streak[0]["source_quote_at"]).total_seconds()
            if len(streak) >= policy.min_samples and duration+policy.clock_jitter_sec >= policy.min_persistence_sec:
                if not sustained_ready:
                    out["sustained"]["events"].append(_event(row, "sustained_setup"))
                    sustained_ready = True
            if seen_above and fell_after_above and previous is not None and previous["price"] < c["trigger_price"]:
                out["recross"]["events"].append(_event(row, "above_below_above_observed"))
                fell_after_above = False
            seen_above = True
        else:
            streak = []
            sustained_ready = False
            if predicate is None:
                seen_above, fell_after_above = False, False
            elif not above and seen_above:
                fell_after_above = True
        if c["route_id"] == ROUTE_E2:
            limit = _number(row["limit_up"])
            if (not _ref(row["limit_evidence_ref"]) or not _ref(c.get("limit_evidence_ref"))
                    or limit is None or limit <= 0 or limit != _number(c.get("limit_up"))):
                e2_open_seen = False
            elif row["price"] < limit-.005:
                e2_open_seen = True
            elif abs(row["price"]-limit) <= .005 and e2_open_seen and predicate is True:
                out["reseal"]["events"].append(_event(row, "observed_open_then_reseal_not_fill"))
                e2_open_seen = False
        previous = row
    if terminated:
        out["sustained"]["current_status"] = "false"
        out["sustained"]["current_reason"] = "candidate_invalidated_no_revival"
    elif rows and (as_of-rows[-1]["source_quote_at"]).total_seconds() <= policy.max_sample_gap_sec:
        last = rows[-1]
        if last["candidate_active"] is True and last["predicate_ref"]:
            out["sustained"]["current_status"] = "true" if sustained_ready else (
                "false" if last["setup_valid"] is False or last["price"] < c["trigger_price"] else "unknown")
    for name, result in out.items():
        if name == "reseal" and c["route_id"] != ROUTE_E2:
            result["reason"] = "not_applicable"
        if result["events"]:
            result["first_event"] = result["events"][0]
            result["status"] = "true"
            result["markouts"] = _markouts(result["first_event"], rows, as_of, policy)
        else:
            result["reason"] = "no_positive_evidence_not_proven_absence"
    return out


def _costs(c, as_of):
    """Costs are separate observations/scenarios; never attach costs to signal fills."""
    out = {"actual_status": "unknown", "actual_fees": None, "actual_quantity": None,
           "hypothetical_order_cost": None, "queue_fill_probability": None,
           "executable_return": None, "net_profit": None}
    out.update(actual_scope="visible_supplied_prefix_not_complete_order_or_cycle",
               visible_fill_count=0, valid_visible_fill_count=0,
               invalid_visible_fill_count=0, future_fill_count=0)
    fills = c.get("actual_fills")
    if isinstance(fills, list) and fills:
        ids, fees, quantity = set(), 0.0, 0
        for fill in fills:
            known = _clock(fill.get("observed_at")) if isinstance(fill, dict) else None
            if known is not None and known > as_of:
                # Cut off BEFORE validating or deduplicating future identities.
                out["future_fill_count"] += 1
                continue
            out["visible_fill_count"] += 1
            if not isinstance(fill, dict):
                out["invalid_visible_fill_count"] += 1
                continue
            at = _clock(fill.get("filled_at"))
            values = [_number(fill.get(k)) for k in ("quantity", "price", "commission", "tax")]
            if (not _ref(fill.get("fill_id")) or fill["fill_id"] in ids
                    or not _ref(fill.get("order_id")) or not _ref(fill.get("evidence_ref"))
                    or fill.get("candidate_id") != c["candidate_id"]
                    or fill.get("code") != c["code"] or not _same_account(fill, c)
                    or fill.get("side") != "buy" or at is None or known is None
                    or not _clock(c["frozen_at"]) <= at <= known <= as_of
                    or at.date() != _clock(c["frozen_at"]).date()
                    or any(v is None for v in values) or values[0] <= 0
                    or not values[0].is_integer() or values[1] <= 0 or min(values[2:]) < 0):
                out["invalid_visible_fill_count"] += 1
                continue
            ids.add(fill["fill_id"])
            quantity += int(values[0])
            fees += values[2]+values[3]
            out["valid_visible_fill_count"] += 1
        if out["valid_visible_fill_count"] and not out["invalid_visible_fill_count"]:
            out.update(actual_status="supplied_fill_evidence_not_independently_reconciled",
                       actual_fees=round(fees, 6), actual_quantity=quantity)
    scenario = c.get("hypothetical_order")
    if isinstance(scenario, dict):
        values = [_number(scenario.get(k)) for k in ("quantity", "limit_price", "commission_rate", "min_commission", "buy_tax_rate")]
        frozen = _clock(scenario.get("frozen_at"))
        if (isinstance(scenario.get("policy_version"), str) and scenario["policy_version"].startswith("research:")
                and frozen is not None and frozen <= _clock(c["frozen_at"])
                and all(v is not None for v in values) and values[0] > 0
                and values[0].is_integer() and values[0] % 100 == 0 and values[1] > 0
                and min(values[2:]) >= 0):
            gross = values[0]*values[1]
            out["hypothetical_order_cost"] = {
                "policy_version": scenario["policy_version"], "quantity": int(values[0]),
                "limit_price": values[1], "gross_order_notional": gross,
                "fee_if_filled": round(max(gross*values[2], values[3])+gross*values[4], 6),
                "charged_fee": None, "fill_assumed": False,
                "cost_basis": "buy_only_conditional_no_exit_fee_or_queue_model"}
    return out


def _entry_shape_contract(c, rows_by_id, candidates_by_id, as_of, policy):
    """Validate a separately frozen hypothesis, never label today's winners ex post.

    This audits supplied event/predicate evidence from existing engines; it does
    not recompute MA, reproduce full selection, or authenticate archive refs.
    """
    shape = c.get("entry_shape")
    if not isinstance(shape, dict):
        return "entry_shape_not_frozen", "unknown"
    family = shape.get("family")
    allowed = {"repair_first_board": (ROUTE_C3,), "first_retest": (ROUTE_A2,),
               "second_ignition": (ROUTE_A2, ROUTE_C)}
    if (not isinstance(family, str) or family not in allowed
            or c["route_id"] not in allowed[family]):
        return "entry_shape_route_unproven", "unknown"
    frozen = _clock(c["frozen_at"])
    cohort_at = _clock(shape.get("cohort_frozen_at"))
    version = shape.get("policy_version")
    if (shape.get("schema") != "entry_shape_hypothesis_v1"
            or not isinstance(version, str) or not version.startswith("research:")
            or _clock(shape.get("frozen_at")) != frozen
            or not _ref(shape.get("evidence_ref")) or not _ref(shape.get("cohort_ref"))
            or cohort_at is None or cohort_at > frozen):
        return "entry_shape_freeze_or_cohort_unproven", "unknown"
    if family == "repair_first_board":
        prior = shape.get("prior_context")
        if not isinstance(prior, dict):
            return "prior_day_structure_missing", "unknown"
        prior_at = _clock(prior.get("observed_at"))
        try:
            previous_day = date.fromisoformat(c.get("previous_trade_date", ""))
        except (ValueError, TypeError):
            return "prior_day_calendar_unproven", "unknown"
        if previous_day >= frozen.date():
            return "prior_day_calendar_unproven", "unknown"
        values = [_number(prior.get(k)) for k in ("close", "ma20")]
        if (prior_at is None or prior_at > frozen
                or not _ref(prior.get("evidence_ref"))
                or not _ref(c.get("previous_trade_date"))
                or prior.get("trade_date") != c["previous_trade_date"]
                or not c["previous_trade_date"] < c["trade_date"]
                or prior.get("price_basis") != c["price_basis"]
                or any(v is None or v <= 0 for v in values)
                or values[0] != c["prev_close"]
                or type(prior.get("limit_streak")) is not int):
            return "prior_day_structure_unproven", "unknown"
        if prior["limit_streak"] != 0:
            return "not_first_board_prior_structure", "not_first_board_control"
        # Split, don't filter away the above-MA20 comparison or future failures.
        return "", "prior_below_ma20" if values[0] < values[1] else "prior_at_or_above_ma20"
    if family == "first_retest":
        anchor = shape.get("armed_anchor")
        if not isinstance(anchor, dict):
            return "first_path_armed_anchor_missing", "unknown"
        source, observed = _clock(anchor.get("source_quote_at")), _clock(anchor.get("observed_at"))
        if (source is None or observed is None or not source <= observed < frozen
                or source.date() != frozen.date() or not _session(source)
                or not _ref(anchor.get("evidence_ref"))
                or not _ref(anchor.get("coverage_ref"))
                or _clock(anchor.get("coverage_end_at")) != frozen):
            return "first_path_coverage_unproven", "unknown"
        return "", "first_path_from_frozen_armed_anchor"
    parent_id = shape.get("parent_candidate_id")
    parent = candidates_by_id.get(parent_id) if isinstance(parent_id, str) else None
    if (parent is None or parent_id == c["candidate_id"] or _candidate_error(parent, as_of)
            or parent["code"] != c["code"] or parent["route_id"] != c["route_id"]
            or not _same_account(parent, c)
            or not _ref(shape.get("new_epoch_ref"))
            or (isinstance(parent.get("entry_shape"), dict)
                and shape["new_epoch_ref"] == parent["entry_shape"].get("new_epoch_ref"))):
        return "second_ignition_new_identity_unproven", "unknown"
    terminal_at = _clock(shape.get("parent_invalidated_at"))
    if terminal_at is None or not terminal_at < frozen or terminal_at.date() != frozen.date():
        return "second_ignition_parent_clock_unproven", "unknown"
    parent_rows, _, fatal, _ = _prepare(parent, rows_by_id.get(parent_id, []), frozen, policy)
    if fatal or not any(r["observed_at"] == terminal_at and r["candidate_active"] is False
            and r.get("shape_stage") == "invalidated"
            and _ref(r.get("shape_event_ref"))
            and r["shape_event_ref"] == shape.get("parent_event_ref") for r in parent_rows):
        return "second_ignition_parent_invalidation_unproven", "unknown"
    return "", "new_epoch_after_observed_invalidation"


def _entry_shape_comparison(c, rows, as_of, policy, *, rows_by_id, candidates_by_id, blocked):
    shape = c.get("entry_shape") if isinstance(c.get("entry_shape"), dict) else {}
    result = {"family": shape.get("family") if shape.get("family") in ENTRY_SHAPE_FAMILIES else "unknown",
              "hypothesis_version": shape.get("policy_version") if _ref(shape.get("policy_version")) else "unknown",
              "status": "unknown", "reason": "", "structure_stratum": "unknown",
              "first_event": None, "path_outcome": "unknown", "markouts": {},
              "comparison_scope": "supplied_frozen_shape_event_audit_not_full_strategy_replay",
              "production_permission": False}
    reason, structure = _entry_shape_contract(c, rows_by_id, candidates_by_id, as_of, policy)
    result.update(reason=reason, structure_stratum=structure)
    if reason or blocked:
        result["reason"] = reason or "sample_evidence_invalid"
        return result
    family = result["family"]
    candidate_seen = rearmed = pulled_back = False
    last = None
    for row in rows:
        if last is not None and (row["source_quote_at"]-last["source_quote_at"]).total_seconds() > policy.max_sample_gap_sec:
            result.update(reason="shape_path_gap_no_reconstruction", path_outcome="coverage_unknown")
            break
        last = row
        stage = row.get("shape_stage")
        if stage is not None and (not isinstance(stage, str) or not _ref(row.get("shape_event_ref"))):
            result.update(reason="shape_event_reference_unproven", path_outcome="coverage_unknown")
            break
        if stage == "sealed" and family == "first_retest" and candidate_seen and not pulled_back:
            result.update(reason="sealed_without_observed_first_retest",
                          path_outcome="no_retest_sealed_control")
            break
        if row["candidate_active"] is False:
            result.update(reason="candidate_invalidated_no_revival", path_outcome="invalidated")
            break
        if row["candidate_active"] is not True:
            result.update(reason="shape_candidate_activity_unproven", path_outcome="coverage_unknown")
            break
        if family == "second_ignition" and stage == "rearmed":
            rearmed = row["setup_valid"] is True and _ref(row["predicate_ref"])
            candidate_seen = False
        if stage == "candidate":
            candidate_seen = family != "second_ignition" or rearmed
        elif stage == "pullback" and candidate_seen:
            pulled_back = True
        if stage == "confirmed":
            path_ok = (family == "repair_first_board"
                       or family == "first_retest" and candidate_seen and pulled_back
                       or family == "second_ignition" and candidate_seen and rearmed)
            if (not path_ok or row["setup_valid"] is not True
                    or not _ref(row["predicate_ref"]) or row["original_trigger"] is not True):
                result.update(reason="shape_confirmation_path_or_current_gate_unproven",
                              path_outcome="confirmation_unknown")
                break
            result.update(status="observed", reason="supplied_event_not_execution",
                          path_outcome="confirmed", first_event=_event(row, family))
            result["markouts"] = _markouts(result["first_event"], rows, as_of, policy)
            break
    if not result["reason"]:
        result.update(reason="no_observed_shape_confirmation",
                      path_outcome="no_confirmed_event_control" if rows else "coverage_unknown")
    return result


def _entry_shape_summary(results, policies):
    groups = {}
    keys = ("trade_date", "account_id", "account_name", "route_id", "production_version",
            "research_version", "hypothesis_version", "family", "structure_stratum",
            "path_outcome", "horizon_minutes", "coverage_reason")
    for item in results:
        c = item["frozen_candidate"] or {}
        comparisons = {r["policy"]["version"]: r for r in item["comparisons"]}
        for policy in policies:
            shape = comparisons.get(policy.version, {}).get("entry_shape", {})
            for minutes in HORIZONS:
                markout = shape.get("markouts", {}).get(str(minutes), {})
                identity = tuple(str(c.get(k)) if c.get(k) is not None else "unknown" for k in keys[:5])
                key = (*identity, policy.version, shape.get("hypothesis_version", "unknown"),
                       shape.get("family", "unknown"), shape.get("structure_stratum", "unknown"),
                       shape.get("path_outcome", "unknown"), minutes,
                       markout.get("reason") or shape.get("reason") or item.get("reason") or "evidence_missing")
                group = groups.setdefault(key, {**dict(zip(keys, key)), "candidate_count": 0,
                    "event_count": 0, "positive_count": 0, "negative_count": 0, "flat_count": 0, "unknown_count": 0})
                group["candidate_count"] += 1
                group["event_count"] += int(shape.get("first_event") is not None)
                value = _number(markout.get("endpoint_markout_pct")) if markout.get("status") == "observed" else None
                outcome = "unknown" if value is None else "positive" if value > 0 else "negative" if value < 0 else "flat"
                group[outcome + "_count"] += 1
    return {"schema": "entry_shape_strata_v1", "families": list(ENTRY_SHAPE_FAMILIES),
            "denominator_basis": "all_supplied_candidates_including_failures_and_unknowns_not_full_market",
            "full_market_recall": None, "production_promotion_allowed": False,
            "strata": [groups[k] for k in sorted(groups)]}


def _evidence_strata(results, policies):
    """Count every supplied candidate per comparison/horizon, never winners only.

    Event time is the first observed trigger, not source quote or daily high.
    Observed coverage means the configured sampled horizon, never tick-complete
    data or a proven fill. Non-events and rejected candidates stay unknown.
    """
    dimensions = ("trade_date", "route_id", "production_version", "research_version",
                  "comparison", "horizon_minutes", "event_session", "coverage",
                  "coverage_reason")
    groups = {}
    for item in results:
        candidate = item["frozen_candidate"] or {}
        identity = [candidate.get(k) if _ref(candidate.get(k)) else "unknown"
                    for k in dimensions[:3]]
        comparisons = {c["policy"]["version"]: c for c in item["comparisons"]}
        for policy in policies:
            comparison = comparisons.get(policy.version)
            for name in ("original", "sustained", "recross", "reseal"):
                route = comparison["routes"][name] if comparison else {}
                event = route.get("first_event")
                at = _clock(event.get("observed_at")) if event else None
                session = _session(at) if at else "no_observed_event"
                for minutes in HORIZONS:
                    markout = route.get("markouts", {}).get(str(minutes), {})
                    observed = markout.get("status") == "observed"
                    reason = (markout.get("reason") or route.get("reason")
                              or item.get("reason") or "evidence_missing")
                    key = (*identity, policy.version, name, minutes, session,
                           "sampled_horizon_observed" if observed else "unknown", reason)
                    if key not in groups:
                        groups[key] = dict(zip(dimensions, key))
                        groups[key].update(candidate_count=0, event_count=0,
                            rejected_candidate_count=0, invalidated_count=0,
                            positive_count=0, negative_count=0, flat_count=0,
                            unknown_count=0)
                    group = groups[key]
                    group["candidate_count"] += 1
                    group["event_count"] += int(event is not None)
                    group["rejected_candidate_count"] += int(item["status"] == "unknown")
                    group["invalidated_count"] += int(
                        route.get("current_reason") == "candidate_invalidated_no_revival")
                    value = _number(markout.get("endpoint_markout_pct")) if observed else None
                    outcome = ("unknown" if value is None else "positive" if value > 0
                               else "negative" if value < 0 else "flat")
                    group[outcome + "_count"] += 1
    return {
        "schema_version": "intraday_evidence_strata_v1",
        "denominator_basis": "each_supplied_candidate_per_policy_comparison_horizon",
        "event_time_basis": "first_event_observed_at_asia_shanghai_session",
        "outcome_basis": "sampled_endpoint_sign_not_trade_win_or_loss",
        "full_market_recall": None,
        "strata": [groups[key] for key in sorted(groups)],
    }


EARLY_LAUNCH_VERSION = "research:early_launch_v1"


def early_launch_variant_contract():
    """One registered audit variant, not a production route or a scanner."""
    policy = RouteResearchPolicy(EARLY_LAUNCH_VERSION, 3, 60, 75, max_source_age_sec=75)
    return {"version": EARLY_LAUNCH_VERSION, "policy": policy.contract(),
            "window": ["09:30:00", "09:35:00"], "window_end_exclusive": True,
            "minimum_change_pct": 3.0, "baseline_route": ROUTE_A2,
            "scope": "supplied_frozen_research_predicate_audit_not_full_selection",
            "production_permission": False, "automatic_promotion_allowed": False,
            "reference_is_fill": False}


def _early_launch_contract_error(c):
    variant = c.get("launch_variant")
    if not isinstance(variant, dict) or variant.get("version") != EARLY_LAUNCH_VERSION:
        return "launch_version_unregistered_or_missing"
    frozen = _clock(c["frozen_at"])
    if c["route_id"] != ROUTE_A2 or not time(9, 30) <= frozen.time() < time(9, 35):
        return "launch_baseline_or_window_unproven"
    for field in ("frozen_at", "cohort_frozen_at", "eligibility_observed_at"):
        at = _clock(variant.get(field))
        if at is None or at > frozen:
            return "launch_point_in_time_unproven"
    if (not all(_ref(variant.get(k)) for k in
                ("evidence_ref", "cohort_ref", "eligibility_ref", "predicate_policy_ref"))
            or variant.get("code") != c["code"]
            or variant.get("trade_date") != c["trade_date"]
            or variant.get("price_basis") != c["price_basis"]
            or variant.get("historical_eligibility_verified") is not True
            or variant.get("sample_role") not in ("design", "failure_control", "prospective")):
        return "launch_cohort_or_historical_identity_unproven"
    return ""


def _early_launch_comparison(c, rows, as_of, policy, blocked):
    out = {"version": EARLY_LAUNCH_VERSION, "status": "unknown", "reason": "",
           "first_event": None, "markouts": {}, "production_permission": False,
           "executable_return": None, "net_profit": None}
    reason = _early_launch_contract_error(c)
    if reason or blocked:
        out["reason"] = reason or "launch_sample_path_unproven"
        return out
    previous = _clock(c["frozen_at"])
    streak = []
    for row in rows:
        source, observed = row["source_quote_at"], row["observed_at"]
        # Signal must be available inside the window, not just quoted before it.
        if observed.time() >= time(9, 35):
            break
        if (source-previous).total_seconds() > policy.max_sample_gap_sec:
            out["reason"] = "launch_coverage_gap_no_reconstruction"
            return out
        previous = source
        if row["candidate_active"] is False:
            out.update(status="control", reason="launch_invalidated_no_revival")
            return out
        if (row["candidate_active"] is not True
                or row.get("launch_version") != EARLY_LAUNCH_VERSION
                or row.get("launch_policy_ref") != c["launch_variant"]["predicate_policy_ref"]
                or not _ref(row.get("launch_predicate_ref"))
                or type(row.get("launch_setup_valid")) is not bool):
            out["reason"] = "launch_predicate_unproven"
            return out
        # An explicit same-source book is necessary, not sufficient for a fill.
        # No day-high/day-low/one-price-day reconstruction or inferred depth.
        values = [_number(row.get(k)) for k in
                  ("ask1_price", "ask1_volume", "bid1_price", "bid1_volume", "limit_up")]
        if (_clock(row.get("book_source_at")) != source
                or not _ref(row.get("book_evidence_ref"))
                or not _ref(row.get("limit_evidence_ref"))
                or any(v is None or v <= 0 for v in values)):
            out["reason"] = "launch_book_or_limit_unproven"
            return out
        ask, _, bid, _, limit = values
        if bid > ask or ask > limit or row["price"] > limit:
            out["reason"] = "launch_book_price_conflict"
            return out
        if ask >= limit or row["price"] >= limit:
            # Known non-executable frame resets, never assume queue execution.
            streak = []
            continue
        if (row["launch_setup_valid"] is not True
                or _pct(row["price"], c["prev_close"]) < 3.0
                or row["price"] < c["trigger_price"]):
            streak = []
            continue
        streak.append(row)
        if (len(streak) >= policy.min_samples
                and (source-streak[0]["source_quote_at"]).total_seconds() >= policy.min_persistence_sec):
            out.update(status="observed", reason="research_confirmation_not_fill",
                       first_event=_event(row, "early_launch_research_confirmation"))
            out["markouts"] = _markouts(out["first_event"], rows, as_of, policy)
            return out
    # A supplied negative prefix is not evidence of complete session absence.
    out["reason"] = "launch_no_confirmed_prefix_not_proven_absence"
    return out


def build_early_launch_report(candidates, samples, *, as_of):
    """Pure opt-in extension of the same frozen-account/cost/markout framework.

    Original A2 events are retained independently, not relaxed or manufactured.
    Caller must supply a pre-frozen independent predicate; this is deliberately
    not a replacement stock selector, execution simulator or policy promotion.
    """
    candidates, samples = list(candidates), list(samples)
    policy = RouteResearchPolicy(EARLY_LAUNCH_VERSION, 3, 60, 75, max_source_age_sec=75)
    report = build_intraday_route_report(candidates, samples, as_of=as_of, policies=[policy])
    at = _clock(as_of)
    counts = {"observed": 0, "control": 0, "unknown": 0}
    for c, item in zip(candidates, report["results"]):
        if item["status"] == "unknown":
            launch = {"version": EARLY_LAUNCH_VERSION, "status": "unknown",
                      "reason": item["reason"], "first_event": None, "markouts": {},
                      "production_permission": False, "executable_return": None, "net_profit": None}
        else:
            rows, _, fatal, _ = _prepare(c, [r for r in samples if isinstance(r, dict)
                                        and r.get("candidate_id") == c["candidate_id"]], at, policy)
            launch = _early_launch_comparison(c, rows, at, policy, fatal)
        item["early_launch"] = _owned(launch)
        counts[launch["status"]] += 1
    report.update(launch_variant=early_launch_variant_contract(),
                  launch_summary={"counts": counts, "candidate_count": len(candidates),
                      "denominator": "all_supplied_candidates_including_failures_and_unknowns",
                      "evaluation_split": "unverified_not_out_of_time",
                      "same_budget_performance": None, "full_market_recall": None})
    report.pop("data_hash")
    report["data_hash"] = _hash(report)
    return report


def build_intraday_route_report(candidates, samples, *, as_of, policies):
    """Owned frozen candidate/sample mappings -> deterministic report; no side effects.

    Mandatory sample predicates are frozen outputs of the existing route gate,
    not guessed from prices. Every quote needs source <= received <= observed.
    Input list order may differ from arrival order; conflicting source/ID
    duplicates or retrograde source arrival block the candidate comparison.
    """
    as_of = _clock(as_of)
    if as_of is None:
        raise ValueError("explicit naive Asia/Shanghai as_of required")
    policies = tuple(policies)
    if not policies or any(not isinstance(p, RouteResearchPolicy) for p in policies):
        raise ValueError("explicit frozen research policies required")
    if len({p.version for p in policies}) != len(policies):
        raise ValueError("research policy versions must be unique")
    candidates, samples = list(candidates), list(samples)
    grouped, unassigned = defaultdict(list), 0
    global_rejected_evidence = []
    for sample in samples:
        if isinstance(sample, dict) and isinstance(sample.get("candidate_id"), str):
            grouped[sample["candidate_id"]].append(sample)
        else:
            unassigned += 1
            global_rejected_evidence.append(_rejected_evidence(sample, "sample_candidate_identity_missing"))
    identities = Counter(c.get("candidate_id") for c in candidates
                         if isinstance(c, dict) and isinstance(c.get("candidate_id"), str))
    orphan_samples = sum(len(rows) for key, rows in grouped.items() if key not in identities)
    global_rejected_evidence.extend(_rejected_evidence(row, "candidate_not_in_frozen_universe")
        for key, rows in grouped.items() if key not in identities for row in rows)
    candidates_by_id = {c["candidate_id"]: c for c in candidates
                        if isinstance(c, dict) and isinstance(c.get("candidate_id"), str)
                        and identities[c["candidate_id"]] == 1}
    results = []
    for c in candidates:
        error = _candidate_error(c, as_of)
        identity = c.get("candidate_id") if isinstance(c, dict) and isinstance(c.get("candidate_id"), str) else None
        if identities.get(identity, 0) > 1:
            error = "duplicate_candidate_identity"
        item = {"candidate_id": identity, "status": "unknown" if error else "evaluated",
                "reason": error, "frozen_candidate": _owned({k: v for k, v in c.items()
                    if k not in {"actual_fills", "hypothetical_order"}}) if isinstance(c, dict) else None,
                "comparisons": []}
        if error:
            item["rejected_candidate_evidence"] = _rejected_evidence(c, error)
            item["rejected_sample_evidence"] = sorted(
                (_rejected_evidence(row, "candidate_rejected") for row in grouped.get(identity, [])),
                key=lambda r: r["raw_leaf_hash"])
        if not error:
            item["costs"] = _costs(c, as_of)
            for policy in policies:
                rows, rejected, fatal, rejected_evidence = _prepare(c, grouped[identity], as_of, policy)
                item["comparisons"].append({"policy": policy.contract(),
                    "status": "unknown" if fatal else "evaluated",
                    "rejections": rejected, "rejected_evidence": rejected_evidence, "sample_count": len(rows),
                    "evidence_hash": _hash({"accepted": rows, "rejected": rejected_evidence}),
                    "routes": _compare(c, rows, as_of, policy, fatal),
                    "entry_shape": _entry_shape_comparison(
                        c, rows, as_of, policy, rows_by_id=grouped,
                        candidates_by_id=candidates_by_id, blocked=fatal)})
        results.append(item)
    report = _owned({"schema_version": "intraday_route_research_v1",
        "input_contract_version": "frozen_route_account_instance_v3", "as_of": as_of,
        "read_only": True, "production_rules_changed": False,
        "scope": "frozen_candidate_confirmation_gate_ablation_not_full_strategy_replay",
        "unassigned_sample_count": unassigned, "orphan_sample_count": orphan_samples, "results": results,
        "evidence_summary": _evidence_strata(results, policies),
        "entry_shape_summary": _entry_shape_summary(results, policies),
        "global_rejected_evidence": sorted(global_rejected_evidence,
            key=lambda r: (r["reason"], r["raw_leaf_hash"])),
        "limitations": ["sampled_quote_markout_not_fill", "no_queue_depth_or_execution_model",
                        "caller_owned_frozen_evidence_not_raw_archive_authentication",
                        "unknown_absence_not_false", "no_historical_policy_backfill",
                         "shape_audit_requires_frozen_cohort_and_existing_engine_event_refs",
                         "generic_recross_is_not_second_ignition",
                         "shape_event_not_new_signal_or_full_strategy_replay"]})
    report["data_hash"] = _hash(report)
    return report


# Opt-in candidate experiment. Never added to SUPPORTED_ROUTES, scanners or settings.
STRATEGY_CANDIDATE_EXPERIMENT_VERSION = "research:strategy_candidate_experiment_20260924_v1"
STRATEGY_CANDIDATE_ROUTES = ("A", "A2", "B", "B2", "C", "C2", "D", "D2",
                             "E", "E2", "F", "F2", "C3")
STRATEGY_CANDIDATE_ACCOUNTS = {
    "A": "default", "A2": "challenger_a", "B": "promotion", "B2": "challenger_b",
    "C": "mainline", "C2": "challenger_c", "D": "auction", "D2": "challenger_d",
    "E": "tenbagger", "E2": "challenger_e", "F": "reversal", "F2": "challenger_f2", "C3": None,
}


def strategy_candidate_experiment_contract():
    """Frozen v1 hypotheses; thresholds are research parameters, not production overrides."""
    return {
        "version": STRATEGY_CANDIDATE_EXPERIMENT_VERSION,
        "production_permission": False, "orders_created": 0, "pushes_created": 0,
        "scope": "supplied_frozen_candidate_prefix_not_full_strategy_or_execution",
        "recent_window_sec": 300, "f_extension_pct": 3.0,
        "default_b2_comparison": "joint_2x30",
        "b2_persistence_variants": {
            "baseline_3x60": {"min_samples": 3, "min_persistence_sec": 60},
            "count_only_2x60": {"min_samples": 2, "min_persistence_sec": 60},
            "duration_only_3x30": {"min_samples": 3, "min_persistence_sec": 30},
            "joint_2x30": {"min_samples": 2, "min_persistence_sec": 30},
        },
        "f_retest": {"min_pullback_pct": 0.5, "max_pullback_pct": 1.8, "min_recovery_pct": 0.3},
        "hypotheses": {
            "A": "Recent observed below-both to above-both transition separates new reclaim from cumulative low.",
            "A2": "Pre-09:35 strong observations expand watch coverage without weakening first-retest confirmation.",
            "B": "Within the original qualified pool, frozen probability ranking exposes the narrow-pool bottleneck; no calibration.",
            "B2": "Two distinct frames over 30 seconds test persistence-window conflict; count-only 2/60 and duration-only 3/30 isolate factors.",
            "C": "Original pool and causal mainline identity remain necessary; missing identity is not a negative.",
            "C2": "Rising recent VWAP and nonnegative market-relative strength exclude weak repairs.",
            "D": "Two independently timed verified auction frames and original source quality must precede any study of signals.",
            "D2": "Verified early/middle/final auction evidence must exist before recovery-shape evaluation.",
            "E": "Original highboard identity plus a recent open/reseal or genuinely new post-cancel round separates old signals.",
            "E2": "A post-cancel confirmation must belong to a new round; open/reseal cannot imply queue execution.",
            "F": "Broken-board identity with route-specific extension/retest branches separates late chasing from repair.",
            "F2": "Broken-board reclaim extended over 3 percent in the observed five-minute window needs a first retest.",
            "C3": "Adding recent positive VWAP slope and sector-relative strength tests repair quality; research only.",
        },
        "sources": [
            "outputs/strategy_effectiveness_audit_20260924/REPORT.md:19-32,66-94",
            "backend/app/paper/strategy_iteration_shadow.py:_reclaim_confirmed,_first_board_confirmation_frame",
            "backend/app/paper/momentum_retest_shadow.py:MomentumRetestPolicy",
            "backend/app/paper/intraday_route_research.py:RouteResearchPolicy,_compare",
        ],
    }


def _experiment_result(value=None, reason="evidence_missing", row=None, **extra):
    return {"status": "unknown" if value is None else "observed" if value else "control",
            "value": value, "reason": reason,
            "first_event": _event(row, reason) if value is True and row else None, **extra}


def _experiment_rows(candidate, samples, as_of, policy):
    """Validate only supplied point-in-time leaves; deduplicate before persistence."""
    rows, rejected, ids, sources = [], Counter(), {}, {}
    fatal = False
    frozen = _clock(candidate["frozen_at"])
    context_start = _clock(candidate.get("context_start_at")) or frozen
    # Select the earliest lawful observation deterministically, never input-list order.
    samples = sorted(samples, key=lambda r: (
        _clock(r.get("observed_at")) or datetime.max,
        _clock(r.get("received_at")) or datetime.max,
        str(r.get("sample_id", "")), _hash(r)) if isinstance(r, dict)
        else (datetime.max, datetime.max, "", _hash(r)))
    for raw in samples:
        if not isinstance(raw, dict):
            rejected["sample_not_object"] += 1
            fatal = True
            continue
        observed = _clock(raw.get("observed_at"))
        if observed and observed > as_of:
            rejected["future_observation_excluded"] += 1
            continue
        source, received = _clock(raw.get("source_quote_at")), _clock(raw.get("received_at"))
        feature_history = raw.get("sample_role") == "feature_history"
        history_allowed = feature_history and candidate.get("route") in ("A", "C2", "C3", "F", "F2")
        reason = ""
        if (not all((source, received, observed)) or not source <= received <= observed <= as_of
                or any(t.date() != frozen.date() for t in (source, received, observed))
                or (observed < context_start and ("context_start_at" in candidate or not history_allowed))
                or not _session(source)
                or _session(source) != _session(received) or _session(source) != _session(observed)
                or (observed-source).total_seconds() > policy.max_source_age_sec):
            reason = "sample_clock_invalid"
        if feature_history and not history_allowed:
            reason = "feature_history_route_not_supported"
        # Optional quote storage clocks cannot be explicitly known-later evidence.
        # Missing commit remains unknown, not a claim of execution availability.
        for key in ("updated_at", "committed_at"):
            if key in raw:
                value = _clock(raw[key])
                if (value is None or received is None or observed is None
                        or not received <= value <= observed):
                    reason = "quote_storage_clock_not_available"
        if "predicate_observed_at" in raw:
            predicate = _clock(raw["predicate_observed_at"])
            if (predicate is None or observed is None or source is None
                    or not observed <= predicate <= as_of
                    or predicate.date() != frozen.date() or _session(predicate) != _session(source)
                    or (predicate-source).total_seconds() > policy.max_source_age_sec
                    or not _ref(raw.get("predicate_evidence_ref"))
                    or raw.get("predicate_evidence_ref") != raw.get("original_gate_ref")
                    or raw.get("sample_role") == "feature_history"):
                reason = "predicate_clock_or_reference_unproven"
        if (raw.get("candidate_id") != candidate["candidate_id"] or not _ref(raw.get("sample_id"))
                or not _ref(raw.get("evidence_ref"))):
            reason = "sample_identity_unproven"
        if any(_number(raw.get(k)) is None or raw[k] <= 0 for k in ("price", "prev_close")):
            reason = "sample_price_invalid"
        if (not all(k in raw for k in ("account_id", "account_name"))
                or raw.get("account_id") != candidate.get("account_id")
                or raw.get("account_name") != candidate.get("account_name")
                or (candidate.get("route") != "C3" and type(raw.get("account_id")) is not int)):
            reason = "sample_account_identity_conflict"
        # Optional adapter identities, if supplied, must agree; no silently mixed stocks/versions.
        for key in ("code", "production_version", "round_id"):
            if key in raw and raw[key] != candidate.get(key):
                reason = "sample_identity_conflict"
        if reason:
            rejected[reason] += 1
            fatal = True
            continue
        row = dict(raw, source_quote_at=source, received_at=received, observed_at=observed)
        fingerprint = _hash({k: v for k, v in row.items()
                             if k not in ("sample_id", "received_at", "observed_at", "evidence_ref")})
        duplicate = False
        for registry, key in ((ids, row["sample_id"]), (sources, source)):
            if key in registry:
                duplicate = True
                if registry[key] != fingerprint:
                    rejected["conflicting_duplicate"] += 1
                    fatal = True
            registry[key] = fingerprint
        if duplicate:
            rejected["duplicate_deduplicated"] += 1
            continue
        rows.append(row)
    rows.sort(key=lambda r: (r["observed_at"], r["source_quote_at"], r["sample_id"]))
    for left, right in zip(rows, rows[1:]):
        if right["source_quote_at"] <= left["source_quote_at"]:
            fatal = True
            rejected["source_arrival_order_conflict"] += 1
        if right["prev_close"] != left["prev_close"]:
            fatal = True
            rejected["price_basis_conflict"] += 1
    return rows, dict(rejected), fatal


def _experiment_offer(row):
    ask, volume = _number(row.get("ask1_price")), _number(row.get("ask1_volume"))
    if ask is None or volume is None:
        return None
    limit = _number(row.get("limit_up"))
    return (ask > 0 and volume > 0
            and (limit is None or (limit > 0 and ask <= limit and row["price"] <= limit)))


def _experiment_identity(candidate, key):
    """Identity must be an explicit frozen boolean, never inferred from later returns."""
    value = candidate.get(key)
    return value if type(value) is bool else None


def _experiment_gate(candidate, row):
    if (not isinstance(candidate.get("gate_inputs"), dict) or not candidate["gate_inputs"]
            or not _ref(candidate.get("gate_ref")) or not _ref(row.get("original_gate_ref"))):
        return None
    value = row.get("original_gate")
    return value if type(value) is bool else None


def _experiment_recent(rows, policy):
    """Return the contiguous current-session prefix tail, never bridge lunch/gaps."""
    if not rows:
        return []
    tail = [rows[-1]]
    for row in reversed(rows[:-1]):
        next_row = tail[-1]
        if (_session(row["source_quote_at"]) != _session(next_row["source_quote_at"])
                or (next_row["source_quote_at"]-row["source_quote_at"]).total_seconds() > policy.max_sample_gap_sec
                or (next_row["observed_at"]-row["observed_at"]).total_seconds() > policy.max_sample_gap_sec
                or (rows[-1]["source_quote_at"]-row["source_quote_at"]).total_seconds() > 300):
            break
        tail.append(row)
    return list(reversed(tail))


def _experiment_sustained(candidate, rows, policy):
    streak = []
    missing = False
    for row in rows:
        # Pre-candidate price history is context, never eligible confirmation time.
        if row["observed_at"] < _clock(candidate["frozen_at"]):
            continue
        gate, offer = _experiment_gate(candidate, row), _experiment_offer(row)
        if gate is None or offer is None:
            missing = True
        if gate is not True or offer is not True:
            streak = []
            continue
        if streak and (
                _session(row["source_quote_at"]) != _session(streak[-1]["source_quote_at"])
                or (row["source_quote_at"]-streak[-1]["source_quote_at"]).total_seconds() > policy.max_sample_gap_sec
                or not 0 < (row["observed_at"]-streak[-1]["observed_at"]).total_seconds() <= policy.max_sample_gap_sec):
            streak = []
        streak.append(row)
    if streak and len(streak) >= policy.min_samples and all(
            (streak[-1][clock]-streak[0][clock]).total_seconds() + policy.clock_jitter_sec
            >= policy.min_persistence_sec for clock in ("source_quote_at", "observed_at")):
        inputs = candidate.get("gate_inputs") or {}
        rules = inputs.get("rule_snapshot", inputs)
        minimum = _number(rules.get("min_vwap_slope_pct")) if isinstance(rules, dict) else None
        first_avg, last_avg = _number(streak[0].get("avg_price")), _number(streak[-1].get("avg_price"))
        if minimum is None or first_avg is None or last_avg is None or min(first_avg, last_avg) <= 0:
            return _experiment_result(reason="frozen_vwap_slope_gate_unproven")
        slope = _pct(last_avg, first_avg)
        return _experiment_result(slope >= minimum, "short_persistence_original_window_and_vwap_retained",
                                  streak[-1], vwap_slope_pct=slope, min_vwap_slope_pct=minimum)
    return _experiment_result(None if missing else False, "short_persistence_not_proven")


def evaluate_strategy_candidate_experiment(candidate, samples, *, as_of, policy=None):
    """Pure v1 experiment on an explicit frozen prefix, not an order or notification.

    Boolean outcomes describe this supplied prefix, never full-day absence. Baseline
    is independently frozen original evidence; candidate and early_observation do
    not overwrite it. See core/CONTRACT.md for route-specific optional fields.
    """
    policy = policy or RouteResearchPolicy("research:b2_2x30_v1", 2, 30, 75)
    if not isinstance(policy, RouteResearchPolicy):
        raise ValueError("RouteResearchPolicy required")
    at = _clock(as_of)
    if at is None:
        raise ValueError("explicit naive Asia/Shanghai as_of required")
    c = candidate if isinstance(candidate, dict) else {}
    route = c.get("route")
    out = {"candidate_id": c.get("candidate_id"), "route": route,
           "version": STRATEGY_CANDIDATE_EXPERIMENT_VERSION, "policy": policy.contract(),
           "production_version": c.get("production_version"), "as_of": at,
           "account_id": c.get("account_id"), "account_name": c.get("account_name"),
           "input_refs": {k: c.get(k) for k in ("evidence_ref", "gate_ref", "frozen_at",
                                               "original_candidate", "original_confirmed_at", "context_start_at")},
           "baseline": _experiment_result(), "candidate": _experiment_result(),
           "early_observation": _experiment_result(reason="not_applicable", is_buy_point=False),
           "production_permission": False, "orders_created": 0, "pushes_created": 0,
           "calibrated_probability": None, "reference_is_fill": False,
           "event_persistence_availability": "unknown", "historically_executable": None,
           "availability_basis": "descriptive_source_received_observed_quote_evidence",
           "sample_count": 0, "rejections": {}}
    out["comparison_label"] = ({(3, 60): "baseline_3x60", (2, 60): "count_only_2x60",
                                (3, 30): "duration_only_3x30", (2, 30): "joint_2x30"}.get(
        (policy.min_samples, policy.min_persistence_sec), "custom_persistence")
        if route == "B2" else "route_specific_overlay_not_short_persistence")
    frozen = _clock(c.get("frozen_at"))
    if (route not in STRATEGY_CANDIDATE_ROUTES
            or not all(_ref(c.get(k)) for k in ("candidate_id", "code", "production_version", "evidence_ref"))
            or not re.fullmatch(r"[0-9]{6}", c.get("code", ""))
            or frozen is None or frozen > at or c.get("trade_date") != frozen.date().isoformat()
            or type(c.get("original_candidate")) is not bool):
        out["candidate"]["reason"] = "candidate_contract_unproven"
        return _owned(out)
    account_valid = (all(k in c for k in ("account_id", "account_name"))
                     and c.get("account_name") == STRATEGY_CANDIDATE_ACCOUNTS[route]
                     and (c.get("account_id") is None if route == "C3"
                          else type(c.get("account_id")) is int and c["account_id"] > 0))
    if not account_valid:
        out["candidate"]["reason"] = "candidate_account_route_unproven"
        return _owned(out)
    if "context_start_at" in c:
        context_start = _clock(c["context_start_at"])
        if context_start is None or context_start > frozen or context_start.date() != frozen.date():
            out["candidate"]["reason"] = "context_clock_unproven"
            return _owned(out)
    baseline_at = _clock(c.get("original_confirmed_at"))
    baseline = c.get("original_confirmed")
    if type(baseline) is bool and (baseline is False or (
            baseline_at is not None and frozen <= baseline_at <= at
            and baseline_at.date() == frozen.date() and _session(baseline_at))):
        out["baseline"] = _experiment_result(baseline, "frozen_original_confirmation",
                                             confirmed_at=baseline_at)
    # Source diagnostics and frozen-pool ranks need no invented intraday quote.
    if route in ("D", "D2"):
        keys = ("two_distinct_verified_frames", "original_source_quality_passed") if route == "D" else (
            "verified_early", "two_distinct_verified_middle", "verified_final")
        contract = c.get("source_contract") if isinstance(c.get("source_contract"), dict) else {}
        evidence_at = _clock(contract.get("evidence_at"))
        observed_at = _clock(contract.get("observed_at"))
        clock_valid = (evidence_at is not None and observed_at is not None
                       and evidence_at <= observed_at <= frozen
                       and evidence_at.date() == observed_at.date() == frozen.date())
        ready = (all(contract.get(k) is True for k in keys)
                 and _ref(contract.get("evidence_ref")) and clock_valid)
        out["candidate"] = _experiment_result(reason="source_contract_only_no_signal")
        out["early_observation"] = _experiment_result(
            True if ready else None, "source_ready_not_buy_point" if ready else "source_contract_unproven",
            is_buy_point=False, missing_fields=[k for k in keys if contract.get(k) is not True]
                + ([] if clock_valid else ["source_contract_clock_unproven"]),
            source_contract_clock_valid=clock_valid)
        return _owned(out)
    if route == "B":
        probability = _number(c.get("probability"))
        eligible = _experiment_identity(c, "pool_identity")
        if eligible is not None and probability is not None and 0 <= probability <= 1:
            out["early_observation"] = _experiment_result(
                eligible and c["original_candidate"], "original_pool_probability_ranking_only", is_buy_point=False,
                ranking_score=probability if eligible and c["original_candidate"] else None)
        out["candidate"] = _experiment_result(reason="probability_calibration_not_established")
        return _owned(out)
    rows, rejects, fatal = _experiment_rows(c, samples, at, policy)
    out.update(sample_count=len(rows), rejections=rejects,
               feature_history_count=sum(r.get("sample_role") == "feature_history" for r in rows))
    if fatal:
        out["candidate"]["reason"] = "path_invalid"
        return _owned(out)
    if not rows:
        out["candidate"]["reason"] = "no_visible_valid_samples"
        return _owned(out)
    last = rows[-1]
    evaluated_at = _clock(last.get("predicate_observed_at")) or last["observed_at"]
    if evaluated_at < frozen or last.get("sample_role") == "feature_history":
        out["candidate"]["reason"] = "context_only_no_candidate_frame"
        return _owned(out)
    recent = _experiment_recent(rows, policy)
    # Evaluation is at the last available observation, not the later report-generation clock.
    out["evaluated_at"] = evaluated_at
    out["evidence_refs"] = [r["evidence_ref"] for r in recent]
    if route == "A2":
        observations = [r for r in rows if time(9, 30) <= r["observed_at"].time() < time(9, 35)
                        and _pct(r["price"], r["prev_close"]) >= 3.0]
        out["early_observation"] = _experiment_result(
            bool(observations), "pre0935_strength_watch_only",
            observations[0] if observations else None, is_buy_point=False)
        # This route deliberately never turns early coverage into a relaxed first retest.
        out["candidate"] = dict(out["baseline"], reason="original_first_retest_unchanged")
        if c["original_candidate"] is not True:
            out["candidate"] = _experiment_result(False, "outside_original_candidate_pool")
        elif out["candidate"]["value"] is True and baseline_at > evaluated_at:
            out["candidate"] = _experiment_result(reason="original_confirmation_after_evaluated_at")
        elif out["candidate"]["value"] is True and (_experiment_offer(last) is not True
                or _experiment_gate(c, last) is not True):
            out["candidate"] = _experiment_result(reason="first_retest_current_gate_or_offer_unproven")
        return _owned(out)
    if c["original_candidate"] is not True:
        out["candidate"] = _experiment_result(False, "outside_original_candidate_pool")
        return _owned(out)
    # Only B2 changes persistence. Quality overlays cannot replace formal confirmation.
    if route != "B2":
        if out["baseline"]["value"] is not True:
            out["candidate"] = _experiment_result(
                False if out["baseline"]["value"] is False else None,
                "original_formal_confirmation_required_for_quality_overlay")
            return _owned(out)
        if baseline_at > evaluated_at:
            out["candidate"] = _experiment_result(reason="original_confirmation_after_evaluated_at")
            return _owned(out)
    offer = _experiment_offer(last)
    gate = _experiment_gate(c, last)
    if offer is not True or gate is not True:
        out["candidate"] = _experiment_result(
            None if offer is None or gate is None else False,
            "offer_or_original_gate_unproven" if offer is None or gate is None else "offer_or_original_gate_failed")
        return _owned(out)
    if route == "B2":
        out["candidate"] = _experiment_sustained(c, rows, policy)
    elif route == "C":
        identities = [_experiment_identity(c, key) for key in ("pool_identity", "mainline_identity")]
        out["candidate"] = _experiment_result(
            None if None in identities else all(identities), "original_pool_mainline_identity", last)
    elif route == "A":
        valid = all(_number(r.get("avg_price")) is not None and r["avg_price"] > 0 for r in recent)
        if len(recent) < 2 or not valid:
            out["candidate"] = _experiment_result(reason="recent_dual_line_path_missing")
        else:
            # The two real crossings may occur on different adjacent frames;
            # neither is inferred from a cumulative daily low.
            crossed = all(any(left["price"] < left[line] and right["price"] >= right[line]
                              for left, right in zip(recent, recent[1:]))
                          for line in ("prev_close", "avg_price"))
            above = last["price"] >= max(last["prev_close"], last["avg_price"])
            out["candidate"] = _experiment_result(crossed and above, "recent_dual_line_reclaim", last)
    elif route in ("C2", "C3"):
        relative_key = "relative_strength_pct" if route == "C2" else "sector_relative_strength_pct"
        relative = _number(last.get(relative_key))
        avgs = [_number(r.get("avg_price")) for r in recent]
        if len(recent) < 2 or any(v is None or v <= 0 for v in avgs) or relative is None:
            out["candidate"] = _experiment_result(reason="repair_quality_evidence_missing")
        else:
            slope = _pct(avgs[-1], avgs[0])
            out["candidate"] = _experiment_result(
                slope > 0 and relative >= 0 and last["price"] >= avgs[-1],
                "recent_positive_vwap_and_relative_repair", last, vwap_slope_pct=slope,
                relative_strength_pct=relative)
    elif route in ("E", "E2"):
        identity = _experiment_identity(c, "highboard_identity")
        cancelled = _clock(c.get("cancelled_at"))
        new_round = (cancelled is not None and cancelled < frozen
                     and cancelled.date() == frozen.date() and _ref(c.get("previous_round_id"))
                     and _ref(c.get("round_id")) and c["round_id"] != c["previous_round_id"])
        limits = [_number(r.get("limit_up")) for r in recent]
        if identity is None:
            out["candidate"] = _experiment_result(reason="highboard_identity_unproven")
        elif not identity:
            out["candidate"] = _experiment_result(False, "not_original_highboard")
        elif c.get("cancelled_at") is not None:
            out["candidate"] = _experiment_result(new_round, "post_cancel_new_round_required", last)
        elif len(recent) < 2 or any(v is None or v <= 0 for v in limits) or len(set(limits)) != 1:
            out["candidate"] = _experiment_result(reason="reseal_path_unproven")
        else:
            opened = any(r["price"] < limits[-1] * 0.998 for r in recent[:-1])
            resealed = last["price"] >= limits[-1] * 0.998
            out["candidate"] = _experiment_result(opened and resealed, "observed_open_reseal_not_fill", last)
    elif route in ("F", "F2"):
        identity = _experiment_identity(c, "broken_board_identity")
        if identity is not True:
            out["candidate"] = _experiment_result(identity, "broken_board_identity_required")
        elif len(recent) < 2:
            out["candidate"] = _experiment_result(reason="extension_path_missing")
        else:
            # Endpoint extension requires an actually observed five-minute window.
            span = (last["source_quote_at"]-recent[0]["source_quote_at"]).total_seconds()
            extension = _pct(last["price"], recent[0]["price"])
            retest = False
            for peak_index in range(len(recent)-2):
                peak = recent[peak_index]["price"]
                for trough in recent[peak_index+1:-1]:
                    pullback = _pct(peak, trough["price"])
                    recovery = _pct(last["price"], trough["price"])
                    if 0.5 <= pullback <= 1.8 and recovery >= 0.3 and last["price"] <= peak:
                        retest = True
            if retest:
                out["candidate"] = _experiment_result(True, "broken_board_observed_retest_branch", last,
                                                       extension_pct=extension)
            elif span < 300 - policy.max_sample_gap_sec:
                out["candidate"] = _experiment_result(reason="five_minute_extension_coverage_missing")
            else:
                out["candidate"] = _experiment_result(extension <= 3.0,
                    "broken_board_nonextended_branch" if extension <= 3.0 else "extended_wait_for_retest",
                    last, extension_pct=extension)
    if out["candidate"]["first_event"] is not None:
        out["candidate"]["first_event"]["predicate_observed_at"] = evaluated_at
    return _owned(out)


def build_strategy_candidate_experiment_report(candidates, samples, *, as_of, policy=None):
    """Batch wrapper; preserves unknowns, rejects duplicate candidate identities."""
    candidates, samples = list(candidates), list(samples)
    counts = Counter(c.get("candidate_id") for c in candidates
                     if isinstance(c, dict) and _ref(c.get("candidate_id")))
    grouped = defaultdict(list)
    for row in samples:
        if isinstance(row, dict) and _ref(row.get("candidate_id")):
            grouped[row["candidate_id"]].append(row)
    results = []
    for c in candidates:
        identity = c.get("candidate_id") if isinstance(c, dict) and _ref(c.get("candidate_id")) else None
        item = evaluate_strategy_candidate_experiment(c, grouped[identity], as_of=as_of, policy=policy)
        if counts[identity] > 1:
            item["candidate"] = _experiment_result(reason="duplicate_candidate_identity")
            item["early_observation"] = _experiment_result(reason="duplicate_candidate_identity", is_buy_point=False)
        results.append(item)
    ranked = sorted((r for r in results if r["route"] == "B"
                     and r["early_observation"].get("ranking_score") is not None),
                    key=lambda r: (-r["early_observation"]["ranking_score"], str(r["candidate_id"])))
    # Ranks belong only to each supplied date/version cohort, never a pooled multi-day probability.
    cohorts = defaultdict(int)
    by_id = {c.get("candidate_id"): c for c in candidates
             if isinstance(c, dict) and _ref(c.get("candidate_id"))}
    for item in ranked:
        c = by_id[item["candidate_id"]]
        key = (c.get("trade_date"), c.get("production_version"))
        cohorts[key] += 1
        item["early_observation"]["supplied_cohort_rank"] = cohorts[key]
    report = {"schema_version": STRATEGY_CANDIDATE_EXPERIMENT_VERSION,
              "as_of": _owned(_clock(as_of)), "contract": strategy_candidate_experiment_contract(),
              "results": results, "candidate_count": len(candidates),
              "production_permission": False, "orders_created": 0, "pushes_created": 0,
              "denominator": "all_supplied_candidates_not_full_market",
              "summary": dict(Counter(r["candidate"]["status"] for r in results))}
    report["data_hash"] = _hash(report)
    return report


def _notification_overlay_clock(value):
    """The frozen notification adapter explicitly uses Shanghai +08 or local naive."""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is not None:
        if value.utcoffset() != timedelta(hours=8):
            return None
        value = value.replace(tzinfo=None)
    return value


def evaluate_strategy_notification_overlay(row, *, as_of):
    """Describe available original-anchor shape components of a notification row.

    Accept route_candidate_notification_input_v1.rows directly. This projection
    NEVER certifies the full candidate: books, identities and relative-strength
    inputs absent from the notification archive remain unproven. Outcome fields
    and notification-anchor features are deliberately never read.
    """
    at = _notification_overlay_clock(as_of)
    if at is None:
        raise ValueError("explicit Shanghai as_of required")
    r = row if isinstance(row, dict) else {}
    route = r.get("route")
    out = {
        "schema_version": "research:notification_component_overlay_20260924_v1",
        "sample_id": r.get("sample_id"), "route": route, "code": r.get("code"),
        "production_version": r.get("production_version"), "evidence_ref": r.get("evidence_ref"),
        "baseline": _experiment_result(reason="notification_fact_unproven"),
        "original_confirmation": _experiment_result(reason="original_anchor_unproven"),
        "feature_overlay": _experiment_result(reason="original_anchor_unproven"),
        "candidate": _experiment_result(reason="full_gate_book_identity_not_reconstructed"),
        "early_observation": _experiment_result(reason="not_reconstructed", is_buy_point=False),
        "component_only": True, "is_buy_point": False, "production_permission": False,
        "orders_created": 0, "pushes_created": 0,
        "event_persistence_availability": "unknown", "historically_executable": None,
        "availability_basis": "descriptive_original_anchor_frozen_features_not_execution",
        "as_of": at, "anchor": None, "feature_window": None,
    }
    if (route not in STRATEGY_CANDIDATE_ROUTES or not _ref(r.get("evidence_ref"))
            or not _ref(r.get("production_version"))):
        out["feature_overlay"]["reason"] = "notification_contract_unproven"
        return _owned(out)
    notified = _notification_overlay_clock(r.get("notification_observed_at"))
    anchor = _notification_overlay_clock(r.get("original_confirmed_at"))
    if (r.get("baseline_confirmed") is True and notified is not None and notified <= at
            and notified.date().isoformat() == r.get("trade_date")):
        out["baseline"] = _experiment_result(True, "original_notification_entry_not_current_buy")
    if (anchor is None or anchor > at or anchor.date().isoformat() != r.get("trade_date")
            or not _session(anchor)):
        return _owned(out)
    features = r.get("original_features")
    if not isinstance(features, dict) or features.get("anchor_known") is not True:
        return _owned(out)
    quote = features.get("current_quote")
    if not isinstance(quote, dict):
        out["feature_overlay"]["reason"] = "original_quote_missing"
        return _owned(out)
    source, received, observed = [_notification_overlay_clock(quote.get(k))
                                 for k in ("source_quote_at", "received_at", "updated_at")]
    if (not all((source, received, observed)) or not source <= received <= observed <= anchor
            or any(t.date() != anchor.date() or _session(t) != _session(anchor)
                   for t in (source, received, observed))
            or (anchor-source).total_seconds() > 180):
        out["feature_overlay"]["reason"] = "original_quote_clock_unproven"
        return _owned(out)
    if quote.get("committed_at") is not None:
        committed = _notification_overlay_clock(quote["committed_at"])
        if committed is None or not observed <= committed <= anchor:
            out["feature_overlay"]["reason"] = "original_quote_commit_not_available_at_anchor"
            return _owned(out)
    out["anchor"] = anchor
    out["original_confirmation"] = _experiment_result(
        True, "supplied_original_anchor_not_transaction_availability", confirmed_at=anchor)
    if route == "A":
        age = _number(features.get("last_cross_age_sec"))
        recent = features.get("recent_reclaim_le300sec")
        above = features.get("current_dual_reclaimed")
        start = _notification_overlay_clock(features.get("segment_start"))
        enough = (start is not None and start.date() == anchor.date()
                  and _session(start) == _session(anchor) and (observed-start).total_seconds() >= 300)
        if recent is True and age is not None and 0 <= age <= 300 and above is True:
            value = True
        elif above is False or (recent is False and enough):
            value = False
        else:
            value = None
        out["feature_overlay"] = _experiment_result(value, "recent_dual_reclaim_component_only")
        out["feature_window"] = "observed_recent_300_seconds_left_censoring_retained"
    elif route == "C2":
        confirmation = r.get("frozen_confirmation")
        confirmation = confirmation if isinstance(confirmation, dict) else {}
        last_at = _notification_overlay_clock(confirmation.get("last_sample_at"))
        first_at = _notification_overlay_clock(confirmation.get("first_sample_at"))
        slope = _number(confirmation.get("vwap_slope_pct"))
        clock_ok = (first_at is not None and last_at is not None
                    and first_at < last_at <= anchor and first_at.date() == anchor.date()
                    and _session(first_at) == _session(last_at) == _session(anchor))
        out["feature_overlay"] = _experiment_result(
            slope > 0 if slope is not None and clock_ok else None,
            "positive_frozen_confirmation_vwap_component_only",
            vwap_slope_pct=slope if clock_ok else None)
        out["feature_window"] = "original_frozen_confirmation_window_not_generic_300s"
    elif route == "C3":
        start = _notification_overlay_clock(features.get("segment_start"))
        slope = _number(features.get("vwap_segment_change_pct"))
        frames = _number(features.get("segment_frames"))
        valid = (start is not None and start < observed and start.date() == anchor.date()
                 and _session(start) == _session(anchor) and frames is not None and frames >= 2)
        out["feature_overlay"] = _experiment_result(
            slope > 0 if slope is not None and valid else None,
            "positive_observed_segment_vwap_component_only", vwap_slope_pct=slope if valid else None)
        out["feature_window"] = "observed_contiguous_segment_not_generic_300s"
    elif route in ("F", "F2"):
        extension = _number(features.get("pre5_pct"))
        ref = features.get("pre5_reference_quote")
        ref = ref if isinstance(ref, dict) else {}
        ref_at, ref_received, ref_observed, ref_committed = [
            _notification_overlay_clock(ref.get(k)) for k in
            ("source_quote_at", "received_at", "updated_at", "committed_at")]
        cutoff = anchor - timedelta(minutes=5)
        valid = (all((ref_at, ref_received, ref_observed, ref_committed))
                 and ref_at <= ref_received <= ref_observed <= ref_committed <= cutoff
                 and all(t.date() == anchor.date() and _session(t) == _session(anchor)
                         for t in (ref_at, ref_received, ref_observed, ref_committed))
                 and 0 <= (cutoff-ref_at).total_seconds() <= 60
                 and 225 <= (source-ref_at).total_seconds() <= 375)
        out["feature_overlay"] = _experiment_result(
            extension <= 3.0 if extension is not None and valid else None,
            "nonextended_component_only_retest_branch_unproven",
            extension_pct=extension if valid else None)
        out["feature_window"] = "original_anchor_pre5_reference"
    elif route == "A2":
        out["feature_overlay"] = _experiment_result(True, "original_first_retest_not_relaxed_component")
        out["feature_window"] = "supplied_original_first_retest_anchor"
    else:
        out["feature_overlay"] = _experiment_result(reason="full_prefix_or_route_identity_required")
    return _owned(out)


def _experiment_v2_freshness(c, baseline, execution_contract, at):
    """Original frozen execution TTL only; freshness is necessary, never permission."""
    anchor = _clock(c.get("original_confirmed_at"))
    out = _experiment_result(
        reason="execution_contract_missing", original_confirmed_at=anchor,
        checked_at=at, max_execution_delay_sec=None, age_sec=None,
        is_buy_point=False, production_permission=False)
    contract = execution_contract
    if not isinstance(contract, dict) or not contract:
        return out
    keys = ("route", "account_id", "account_name", "execution_strategy_version")
    if (contract.get("schema") != "candidate_shadow_execution_freshness_v1"
            or any(key not in contract or key not in c or contract[key] != c[key] for key in keys)
            or not _ref(contract.get("execution_strategy_version"))
            or type(contract.get("account_id")) is not int
            or type(c.get("account_id")) is not int or c["account_id"] <= 0):
        out["reason"] = "execution_contract_identity_unproven"
        return out
    ttl = _number(contract.get("max_execution_delay_sec"))
    policy_at = _clock(contract.get("policy_observed_at"))
    candidate_frozen = _clock(c.get("frozen_at"))
    if (ttl is None or ttl <= 0 or anchor is None
            or policy_at is None or policy_at > at or candidate_frozen is None
            or not candidate_frozen <= anchor <= at
            or c.get("trade_date") != anchor.date().isoformat()
            or not _session(anchor)):
        out["reason"] = "execution_contract_clock_or_ttl_unproven"
        return out
    out.update(max_execution_delay_sec=ttl, age_sec=(at-anchor).total_seconds(),
               policy_observed_at=policy_at,
               policy_basis="startup_frozen_original_execution_policy_not_historical_policy_proof")
    invalidated = _clock(c.get("confirmation_invalidated_at"))
    if "confirmation_invalidated_at" in c and (invalidated is None or invalidated > at):
        value, reason = None, "confirmation_invalidation_clock_unproven"
    elif invalidated is not None and anchor <= invalidated:
        value, reason = False, "original_confirmation_invalidated"
    elif baseline.get("value") is not True:
        value, reason = baseline.get("value"), "original_confirmation_not_proven"
    elif at.date() != anchor.date():
        value, reason = False, "original_confirmation_cross_day"
    elif _session(at) != _session(anchor):
        value, reason = False, "original_confirmation_cross_session"
    elif (at-anchor).total_seconds() > ttl:
        value, reason = False, "original_confirmation_expired"
    else:
        value, reason = True, "original_confirmation_within_frozen_ttl_not_buy_permission"
    out.update(status="unknown" if value is None else "observed" if value else "control",
               value=value, reason=reason)
    return out


def evaluate_strategy_candidate_experiment_v2(
        candidate, samples, *, as_of, original_result=None, execution_contract=None, policy=None):
    """Opt-in C2 hypothesis on a visible prefix; v1 is never overwritten.

    execution_contract schema is candidate_shadow_execution_freshness_v1 and
    binds route/account_id/account_name/execution_strategy_version, an explicit
    max_execution_delay_sec and actual startup policy_observed_at <= as_of.
    Policy capture may be on a prior day; it never refreshes original_confirmed_at.
    This tests today's frozen execution policy, not historical policy provenance.
    The caller, not this pure function, is responsible for freezing those facts.
    No default production TTL, account lookup, DB, IO or execution is performed.

    original_result, when supplied, is the exact v1 result for the visible prefix,
    not a producer boolean. A mismatch fails the added research layer closed.
    Future observations (including not-yet-visible predicate evaluations) are
    excluded before either evaluation, so appending the future changes nothing.

    C2 requires an unexpired original confirmation, current original gate/offer,
    policy-bounded distinct-source persistence, adjacent nondecreasing VWAP and
    price, and a strictly higher last price than first. No amplitude is optimized
    on this day's winners. A flat VWAP is allowed; a flat price is not.
    Other routes expose unchanged v1 diagnostics only, never a newly invented buy.
    """
    at = _clock(as_of)
    if at is None:
        raise ValueError("explicit naive Asia/Shanghai as_of required")
    if policy is None:
        policy = RouteResearchPolicy("research:b2_2x30_v1", 2, 30, 75)
    if not isinstance(policy, RouteResearchPolicy):
        raise ValueError("RouteResearchPolicy required")
    c = candidate if isinstance(candidate, dict) else {}
    visible = []
    for row in samples:
        if isinstance(row, dict):
            observed = _clock(row.get("observed_at"))
            predicate = _clock(row.get("predicate_observed_at"))
            if (observed is not None and observed > at) or (predicate is not None and predicate > at):
                continue
        visible.append(row)
    out = evaluate_strategy_candidate_experiment(c, visible, as_of=at, policy=policy)
    freshness = _experiment_v2_freshness(c, out["baseline"], execution_contract, at)
    research = _experiment_result(reason="v1_diagnostic_only_no_new_algorithm",
                                  is_buy_point=False, production_permission=False)
    out.update(research_v2=research, candidate_v2=research, confirmation_freshness=freshness,
               research_v2_version="research:strategy_candidate_experiment_20260924_v2",
               research_v2_scope="observation_only_not_12_account_production",
               v1_diagnostic=dict(out["candidate"]))
    if original_result is not None and _owned(original_result) != _owned({
            k: v for k, v in out.items() if k not in (
                "research_v2", "candidate_v2", "confirmation_freshness", "research_v2_version",
                "research_v2_scope", "v1_diagnostic")}):
        research["reason"] = "original_result_mismatch"
        return _owned(out)
    if c.get("route") != "C2":
        return _owned(out)
    # An invalid candidate/path must never obtain a freshness-based rescue.
    if "evaluated_at" not in out:
        research["reason"] = out["candidate"]["reason"]
        return _owned(out)
    if _experiment_identity(c, "original_candidate") is not True:
        research.update(_experiment_result(False, "outside_original_candidate_pool"))
        return _owned(out)
    if freshness["value"] is not True:
        research.update(_experiment_result(freshness["value"], freshness["reason"]))
        return _owned(out)
    rows, _, fatal = _experiment_rows(c, visible, at, policy)
    if fatal or not rows:
        research["reason"] = "path_invalid"
        return _owned(out)
    last = rows[-1]
    anchor = _clock(c["original_confirmed_at"])
    # Supplied invalidations cannot be revived by later good quotes with the old anchor.
    invalidations = [r for r in rows if (
        r.get("candidate_active") is False or r.get("stage") in ("reset", "cancelled", "invalidated")
        or r.get("producer_reset") is True)]
    if any(r["observed_at"] >= anchor for r in invalidations):
        research.update(_experiment_result(False, "original_confirmation_invalidated"))
        return _owned(out)
    if (_session(last["source_quote_at"]) != _session(at)
            or last["source_quote_at"].date() != at.date()
            or (at-last["source_quote_at"]).total_seconds() > policy.max_source_age_sec
            or anchor > (_clock(last.get("predicate_observed_at")) or last["observed_at"])):
        research["reason"] = "current_quote_or_confirmation_clock_unproven"
        return _owned(out)
    gate, offer = _experiment_gate(c, last), _experiment_offer(last)
    if gate is not True or offer is not True:
        research.update(_experiment_result(
            None if gate is None or offer is None else False,
            "offer_or_original_gate_unproven" if gate is None or offer is None
            else "offer_or_original_gate_failed"))
        return _owned(out)
    recent = _experiment_recent(rows, policy)
    if invalidations:
        cutoff = invalidations[-1]["observed_at"]
        recent = [r for r in recent if r["observed_at"] > cutoff]
    # Never bridge supplied reset/gap generations even when quote clocks are close.
    for key in ("episode_id", "stream_id", "generation"):
        supplied = [r.get(key) for r in recent if key in r]
        if supplied and (len(supplied) != len(recent) or any(v != supplied[0] for v in supplied)):
            research["reason"] = "path_generation_unproven"
            return _owned(out)
    if (len(recent) < max(2, policy.min_samples) or any(
            (recent[-1][clock]-recent[0][clock]).total_seconds() + policy.clock_jitter_sec
            < policy.min_persistence_sec for clock in ("source_quote_at", "observed_at"))):
        research["reason"] = "recent_distinct_persistent_path_missing"
        return _owned(out)
    avgs = [_number(r.get("avg_price")) for r in recent]
    relative = _number(last.get("relative_strength_pct"))
    if any(v is None or v <= 0 for v in avgs) or relative is None:
        research["reason"] = "repair_quality_evidence_missing"
        return _owned(out)
    prices = [r["price"] for r in recent]
    vwap_nondecreasing = all(right >= left for left, right in zip(avgs, avgs[1:]))
    price_progress = (prices[-1] > prices[0]
                      and all(right >= left for left, right in zip(prices, prices[1:])))
    value = (vwap_nondecreasing and price_progress and relative >= 0 and prices[-1] >= avgs[-1])
    research.update(_experiment_result(
        value, "c2_nondecreasing_vwap_with_observed_price_progress", last,
        vwap_nondecreasing=vwap_nondecreasing, price_progress=price_progress,
        vwap_slope_pct=_pct(avgs[-1], avgs[0]), relative_strength_pct=relative,
        distinct_source_samples=len(recent), original_confirmed_at=anchor,
        evaluated_at=at, evidence_refs=[r["evidence_ref"] for r in recent]))
    if research["first_event"] is not None:
        research["first_event"]["predicate_observed_at"] = _clock(out["evaluated_at"])
    return _owned(out)


def evaluate_c3_confirmation_momentum_experiment(candidate, samples, *, as_of, policy):
    """Opt-in C3 quality overlay on the ORIGINAL complete confirmation window.

    No live scanner/notification registration. Frozen level gates are necessary,
    but do not imply positive incremental momentum. Test price progress and
    non-deteriorating frozen same-sector relative strength at a fixed zero
    threshold, without re-anchoring, requiring rising VWAP, or fitting outcomes.
    Supplied evidence is not authenticated transaction availability or a fill.
    """
    if not isinstance(policy, RouteResearchPolicy):
        raise ValueError("frozen RouteResearchPolicy required")
    at = _clock(as_of)
    if at is None:
        raise ValueError("explicit naive Asia/Shanghai as_of required")
    c = candidate if isinstance(candidate, dict) else {}
    anchor = _clock(c.get("original_confirmed_at"))
    cutoff = min(anchor, at) if anchor else at
    visible = []
    for row in samples:
        if isinstance(row, dict):
            observed = _clock(row.get("observed_at"))
            predicate = _clock(row.get("predicate_observed_at"))
            if (observed is not None and observed > cutoff) or (
                    predicate is not None and predicate > cutoff):
                continue
        visible.append(row)
    # Existing v1 comparator and all original identity/clock checks are retained.
    out = evaluate_strategy_candidate_experiment(c, visible, as_of=cutoff, policy=policy)
    added = _experiment_result(reason="original_c3_confirmation_unproven",
                               is_buy_point=False, production_permission=False)
    out.update(momentum_candidate=added,
               momentum_version="research:c3_confirmation_momentum_20260929_v1",
               momentum_scope="original_window_quality_overlay_not_full_strategy_or_execution")
    if (c.get("route") != "C3" or c.get("original_candidate") is not True
            or out["baseline"]["value"] is not True or "evaluated_at" not in out
            or anchor is None or anchor > at):
        return _owned(out)
    rows, _, fatal = _experiment_rows(c, visible, cutoff, policy)
    window = c.get("confirmation_window")
    if fatal or not rows or not isinstance(window, dict):
        added["reason"] = "original_window_missing_or_invalid"
        return _owned(out)
    first, last = _clock(window.get("first_sample_at")), _clock(window.get("last_sample_at"))
    counts = [window.get(k) for k in ("sample_count", "source_sample_count")]
    bound = (
        first is not None and first < anchor and last == anchor
        and rows[0]["observed_at"] == first and rows[-1]["observed_at"] == anchor
        and all(type(n) is int and n == len(rows) for n in counts)
        and len(rows) >= max(2, policy.min_samples)
        and window.get("min_samples") == policy.min_samples
        and window.get("min_persistence_sec") == policy.min_persistence_sec
        and window.get("max_sample_gap_sec") == policy.max_sample_gap_sec
        and _number(c.get("reference_price")) == rows[-1]["price"]
        and all(r.get("code") == c.get("code")
                and r.get("production_version") == c.get("production_version") for r in rows)
    )
    if not bound:
        added["reason"] = "original_window_binding_or_coverage_unproven"
        return _owned(out)
    if any(
        _session(r["source_quote_at"]) != _session(anchor)
        or _session(r["observed_at"]) != _session(anchor)
        for r in rows
    ) or any(
        not 0 < (right[clock]-left[clock]).total_seconds() <= policy.max_sample_gap_sec
        for left, right in zip(rows, rows[1:])
        for clock in ("observed_at", "source_quote_at")
    ) or any(
        (rows[-1][clock]-rows[0][clock]).total_seconds() + policy.clock_jitter_sec
        < policy.min_persistence_sec for clock in ("observed_at", "source_quote_at")
    ):
        added["reason"] = "original_window_continuity_unproven"
        return _owned(out)
    # A missing/failed original frame contradicts a complete confirmation proof.
    # It is unknown coverage, not a fabricated negative training example.
    if any(_experiment_gate(c, r) is not True or _experiment_offer(r) is not True for r in rows):
        added["reason"] = "original_window_gate_or_offer_unproven"
        return _owned(out)
    sector = c.get("sector_code")
    relative = [_number(r.get("sector_relative_strength_pct")) for r in rows]
    if not _ref(sector) or any(r.get("sector_code") != sector for r in rows) or any(
            value is None for value in relative):
        added["reason"] = "same_sector_relative_path_unproven"
        return _owned(out)
    price_progress = rows[-1]["price"] > rows[0]["price"]
    relative_not_fading = relative[-1] >= relative[0]
    added.update(_experiment_result(
        price_progress and relative_not_fading,
        "original_window_price_progress_and_nonfading_relative_strength",
        rows[-1], price_progress=price_progress, relative_not_fading=relative_not_fading,
        window_price_return_pct=_pct(rows[-1]["price"], rows[0]["price"]),
        relative_strength_change_pct=relative[-1]-relative[0],
        distinct_source_samples=len(rows), original_confirmed_at=anchor,
        first_sample_at=first, evaluated_at=anchor,
        evidence_refs=[r["evidence_ref"] for r in rows],
        sector_clock_basis="supplied_frozen_sector_context_not_supplier_freshness_certificate"))
    return _owned(out)
