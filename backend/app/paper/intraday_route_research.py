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
