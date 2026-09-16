"""Pure supplied-evidence post-trim research; no IO, orders or production entry.

Contract v1: a case owns identity, anchor and ordered frames. Identity consists of
cycle_id/account_instance/code/buy_time/entry_version. Anchor binds before/after
ledger revisions, a confirmed unique sell-fill summary and its separate clocks.
Each frame binds that same identity/revision/remaining quantity and original gate
evidence. observed_state_content_hash is informational, NEVER a ledger revision.

All references are caller-supplied evidence, not independently authenticated.
Missing history is unknown, never reconstructed from inventory or session high.
"""
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
import re

from app.paper.exit_audit import finite_profit_pct
from app.paper.profit_protection_research import (
    _calendar_day, _clock, _day, _hash, _int, _num, _owned, _ref,
    _sell_date, _session, _threshold_reached,
)

SCHEMA = "research:post_trim_v1"
IDENTITY = ("cycle_id", "account_instance", "code", "buy_time", "entry_version")
CHECKS = ("t1", "quantity", "quote", "orderbook", "other_original_blocks")
HARD = ("hard_stop", "max_hold", "frozen_forced_exit")


@dataclass(frozen=True)
class PostTrimPolicy:
    version: str
    deterioration_pct: float
    max_quote_age_sec: int
    max_sample_gap_sec: int
    unit: str = "percent_of_segment_first_price"

    def __post_init__(self):
        if not _ref(self.version) or not self.version.startswith("research:"):
            raise ValueError("explicit research: version required")
        value = _num(self.deterioration_pct)
        if value is None or not 0 < value < 100 or self.unit != "percent_of_segment_first_price":
            raise ValueError("finite positive percent threshold in (0,100) required")
        if not _int(self.max_quote_age_sec, 1) or not _int(self.max_sample_gap_sec, 1):
            raise ValueError("positive integer seconds required")


def _identity(value):
    return (type(value) is dict and all(_ref(value.get(k)) for k in IDENTITY)
            and re.fullmatch("[0-9]{6}", value["code"]) is not None
            and _clock(value["buy_time"]) is not None)


def _same(a, b):
    return type(a) is dict and all(type(a.get(k)) is type(b.get(k))
                                 and a.get(k) == b.get(k) for k in IDENTITY)


def _positive(value):
    return _num(value) is not None and value > 0


def _anchor_error(a, ident, as_of):
    if not _identity(ident) or type(a) is not dict or not _same(a.get("identity"), ident):
        return "anchor_identity_unknown"
    if (a.get("revision_basis") != "supplied_immutable_ledger_transition"
            or not all(_ref(a.get(k)) for k in ("before_revision", "after_revision",
                                               "transition_ref", "order_id", "fill_summary_ref"))
            or a["before_revision"] == a["after_revision"]
            or a.get("first_trim_in_cycle") is not True
            or a.get("fill_summary_complete") is not True
            or a.get("status") != "confirmed_partial_position"):
        return "confirmed_first_transition_unknown"
    if (not _int(a.get("before_quantity"), 1) or not _int(a.get("remaining_quantity"), 1)
            or not _int(a.get("filled_quantity"), 1)
            or a["before_quantity"] != a["remaining_quantity"] + a["filled_quantity"]):
        return "partial_quantity_not_conserved"
    response, commit = _clock(a.get("response_observed_at")), _clock(a.get("commit_known_at"))
    fills = a.get("fills")
    if response is None or commit is None or not response <= commit <= as_of:
        return "anchor_confirmation_clock_unknown"
    if type(fills) is not list or not fills:
        return "confirmed_fills_missing"
    seen, total, last = set(), 0, None
    for f in fills:
        if (type(f) is not dict or not _same(f.get("identity"), ident)
                or f.get("order_id") != a["order_id"] or f.get("side") != "sell"
                or not _ref(f.get("fill_id")) or f["fill_id"] in seen
                or not _ref(f.get("evidence_ref")) or not _int(f.get("quantity"), 1)
                or not _positive(f.get("price"))):
            return "fill_identity_or_quantity_invalid"
        at = _clock(f.get("occurred_at"))
        if at is None or not _clock(ident["buy_time"]) <= at <= response:
            return "fill_occurrence_clock_invalid"
        seen.add(f["fill_id"])
        total += f["quantity"]
        last = max(last, at) if last else at
    before_known = _clock(a.get("before_revision_known_at"))
    if (before_known is None or before_known < _clock(ident["buy_time"])
            or any(before_known > _clock(f["occurred_at"]) for f in fills)):
        return "before_revision_clock_unknown"
    if total != a["filled_quantity"] or _clock(a.get("occurred_at")) != last:
        return "fill_aggregate_conflict"
    return ""


def _gate(frame, ident, observed):
    """Validate explicit frozen original output, never infer hard class from prose."""
    g = frame.get("original_gate")
    if type(g) is not dict:
        return None
    if (not _same(g.get("identity"), ident) or not _same(frame.get("identity"), ident)
            or g.get("revision") != frame.get("revision")
            or g.get("round_id") != frame.get("round_id")
            or g.get("basis") != "supplied_complete_frozen_original_evaluation"
            or not all(_ref(g.get(k)) for k in ("evidence_ref", "policy_ref", "policy_revision", "revision", "round_id"))
            or g.get("entry_version") != ident["entry_version"]
            or type(g.get("triggered")) is not bool
            or g.get("exit_class") not in (*HARD, "nonhard_protection", "none")):
        return None
    known = _clock(g.get("known_at"))
    frozen = _clock(g.get("policy_frozen_at"))
    rules = g.get("frozen_rules")
    if (known is None or observed is None or not _clock(ident["buy_time"]) <= known <= observed
            or frozen is None or frozen > _clock(ident["buy_time"])
            or type(rules) is not dict or not _int(rules.get("max_hold_days"), 1)
            or not all(_positive(rules.get(k)) for k in ("stop_loss_pct", "take_profit_pct"))
            or type(rules.get("forced_exit_rule_ids")) is not list
            or not all(_ref(v) for v in rules["forced_exit_rule_ids"])
            or g.get("rules_content_hash") != _hash(rules)
            or g.get("missing_keys") != []):
        return None
    checks = g.get("checks")
    if (type(checks) is not dict or set(checks) != set(CHECKS)
            or any(type(checks[k]) is not dict
                   or type(checks[k].get("passed")) is not bool
                   or not _ref(checks[k].get("evidence_ref")) for k in CHECKS)):
        return None
    if g["exit_class"] in HARD and not _ref(g.get("hard_proof_ref")):
        return None
    if (g["exit_class"] == "frozen_forced_exit"
            and g.get("matched_forced_rule_id") not in rules["forced_exit_rule_ids"]):
        return None
    if g["triggered"] and g["exit_class"] == "none":
        return None
    return g


def _case(case, calendar, as_of, policy, duplicate=False):
    case = case if type(case) is dict else {}
    ident, a = case.get("identity"), case.get("anchor")
    error = "duplicate_case_identity" if duplicate else _anchor_error(a, ident, as_of)
    frames = case.get("frames")
    if type(frames) is not list:
        frames = [None]  # missing evidence remains in the frame denominator
    output, seen, rounds = [], {}, {}
    previous = segment_price = last_observed = highest_source = None
    terminated = False
    baseline_at = candidate_at = None
    for raw in frames:
        f = raw if type(raw) is dict else {}
        observed = _clock(f.get("observed_at"))
        source = _clock(f.get("source_quote_at"))
        row = {"sample_ref": f.get("evidence_ref") if _ref(f.get("evidence_ref")) else None,
               "baseline": "unknown", "candidate": "unknown", "reason": "",
               "source_after_confirmation_seconds": None, "deterioration_pct": None,
               "original_gate_evidence": _owned(f.get("original_gate")),
               "observed_state_content_hash": f.get("observed_state_content_hash"),
               "ledger_revision": f.get("revision")}
        output.append(row)

        def reject(reason):
            nonlocal previous, segment_price
            row["reason"] = reason
            previous = segment_price = None

        # Hard intent is preserved independently of candidate anchor/price completeness.
        # Its original blocks remain attached; this never means permission to execute.
        gate = _gate(f, ident, observed) if _identity(ident) else None
        if observed is None or observed > as_of:
            reject("future_or_unknown_observation")
            continue
        observation_rollback = last_observed is not None and observed < last_observed
        last_observed = max(last_observed, observed) if last_observed else observed
        row["observation_order_rollback"] = observation_rollback
        received = _clock(f.get("received_at"))
        quote_clock_valid = (
            source is not None and received is not None
            and source <= received <= observed and source.date() == observed.date()
            and _session(source) is not None and _session(source) == _session(observed)
            and _session(received) == _session(source)
            and (observed-source).total_seconds() <= policy.max_quote_age_sec)
        # Clock-valid negative/unknown evidence is still an observed source boundary.
        # Capture the PRIOR boundary: comparing against the just-advanced watermark
        # would reject every new source and prevent recovery forever.
        source_boundary_before = highest_source
        round_reuse = False
        if quote_clock_valid and not observation_rollback:
            highest_source = max(highest_source, source) if highest_source else source
            # Negative/unknown and hard frames still establish a round's source
            # identity. Do not let a later frame recycle that round with a new
            # source clock just because its first business evaluation was not ready.
            if _ref(f.get("round_id")) and _ref(f.get("source")):
                round_key = (f["source"], source)
                round_reuse = (f["round_id"] in rounds
                               and rounds[f["round_id"]] != round_key)
                if not round_reuse:
                    rounds[f["round_id"]] = round_key
        fi = f.get("identity")
        if not error:
            if (type(fi) is dict and any(k in fi and fi[k] is not None and
                    (type(fi[k]) is not type(ident[k]) or fi[k] != ident[k]) for k in IDENTITY)):
                terminated = True
            if (f.get("revision") is not None and f["revision"] != a["after_revision"]
                    or type(f.get("remaining_quantity")) is int
                    and f["remaining_quantity"] != a["remaining_quantity"]
                    or f.get("position_active") is False):
                terminated = True
        if gate:
            row["baseline"] = ("true" if gate["triggered"] and
                               gate["exit_class"] == "nonhard_protection" else "false")
            if gate["triggered"] and gate["exit_class"] in HARD:
                row["candidate"] = "preserve_original_hard_exit"
                reject("original_hard_path_with_original_blocks_unchanged")
                continue
        if row["baseline"] == "true" and baseline_at is None:
            baseline_at = observed
        if error:
            reject(error)
            continue
        # Missing leaves break a segment; positive evidence of a changed cycle,
        # revision, quantity or close permanently retires this anchor.
        if terminated:
            reject("anchor_invalidated_by_identity_revision_quantity_or_close")
            continue
        if (not _same(fi, ident) or f.get("revision") != a["after_revision"]
                or type(f.get("remaining_quantity")) is not int
                or f.get("position_active") is not True):
            reject("frame_identity_revision_or_quantity_unknown")
            continue
        if observation_rollback:
            reject("observation_order_rollback")
            continue
        if round_reuse:
            reject("same_round_reuse")
            continue
        if gate is None:
            reject("complete_original_gate_unknown")
            continue
        if any(not gate["checks"][k]["passed"] for k in CHECKS):
            row["candidate"] = "blocked_by_original_gate"
            reject("original_blocks_preserved")
            continue
        if (not all(_ref(a.get(k)) for k in ("entry_policy_revision", "entry_policy_ref", "entry_rules_content_hash"))
                or a["entry_policy_revision"] != gate["policy_revision"]
                or a["entry_policy_ref"] != gate["policy_ref"]
                or a["entry_rules_content_hash"] != gate["rules_content_hash"]):
            reject("frozen_entry_policy_binding_unknown")
            continue
        state_at = _clock(f.get("revision_known_at"))
        if (not quote_clock_valid or state_at is None
                or not _clock(a["commit_known_at"]) <= state_at <= observed):
            reject("quote_or_revision_clock_invalid")
            continue
        row["source_after_confirmation_seconds"] = (source-_clock(a["commit_known_at"])).total_seconds()
        if source <= _clock(a["commit_known_at"]):
            reject("source_not_strictly_after_confirmation")
            continue
        if (not all(_ref(f.get(k)) for k in ("round_id", "evidence_ref", "quantity_ref",
                                            "basis_ref", "price_basis", "source"))
                or f.get("source") != a.get("source")
                or f.get("price_basis") != a.get("price_basis")
                or f.get("corporate_action_status") != "none"
                or a.get("corporate_action_status") != "none"
                or not _ref(a.get("basis_ref")) or not _positive(f.get("price"))):
            reject("price_basis_or_source_unknown")
            continue
        first, cal_error = _sell_date(_clock(ident["buy_time"]).date(), source.date(), observed, calendar)
        if cal_error or _calendar_day(source.date(), observed, calendar) is not True:
            reject("calendar_unknown")
            continue
        if first is None or source.date() <= _clock(ident["buy_time"]).date():
            row["candidate"] = "blocked_by_original_gate"
            reject("t_plus_one")
            continue
        if (source.date() != _clock(a["occurred_at"]).date()
                or a["filled_quantity"] < 100):
            reject("original_same_day_trim_100_share_context_unproven")
            continue
        basis_known = _clock(f.get("basis_known_at"))
        anchor_basis_known = _clock(a.get("basis_known_at"))
        if (basis_known is None or basis_known > observed
                or anchor_basis_known is None or anchor_basis_known > _clock(a["commit_known_at"])):
            reject("price_basis_first_knowledge_unknown")
            continue
        q = f.get("sellable_quantity")
        if not _int(q) or q > a["remaining_quantity"]:
            reject("legal_quantity_unknown")
            continue
        if q < 100 or q % 100:
            row["candidate"] = "blocked_by_original_gate"
            reject("existing_100_share_gate")
            continue
        quote_binding = {k: f.get(k) for k in ("source", "source_quote_at", "price", "round_id")}
        if (gate.get("quote_content_hash") != _hash(quote_binding)
                or type(gate.get("evaluated_quantity")) is not int
                or gate["evaluated_quantity"] != q or not _positive(gate.get("execution_price"))
                or not received <= _clock(gate["known_at"]) <= observed):
            reject("original_execution_quantity_quote_or_clock_binding_unknown")
            continue
        key = (f["source"], source)
        fingerprint = _hash({k: f.get(k) for k in ("price", "price_basis", "revision", "remaining_quantity")})
        if source_boundary_before is not None and source < source_boundary_before:
            reject("source_rollback")
            continue
        if key in seen:
            if seen[key] == fingerprint:
                row["reason"] = "same_source_duplicate_not_new_sample"
            else:
                reject("source_conflict")
            continue
        seen[key] = fingerprint
        if source_boundary_before is not None and source <= source_boundary_before:
            reject("source_rollback")
            continue
        gap = (previous is not None and
               (source.date() != previous.date() or _session(source) != _session(previous)
                or (source-previous).total_seconds() > policy.max_sample_gap_sec))
        if gap:
            previous = segment_price = None
        if previous is None:
            previous, segment_price = source, f["price"]
            row["reason"] = "new_segment_seed_after_gap" if gap else "new_segment_seed"
            row["candidate"] = "waiting" if row["baseline"] == "true" else "not_applicable"
            continue
        if row["baseline"] != "true":
            previous = source
            row["candidate"] = "not_applicable"
            row["reason"] = "original_nonhard_trigger_required"
            continue
        change = finite_profit_pct(f["price"], segment_price)
        if change is None:
            reject("derived_profit_nonfinite")
            continue
        row["deterioration_pct"] = -change
        previous = source
        if _threshold_reached(-change, policy.deterioration_pct):
            row["candidate"] = "research_condition_met"
            row["reason"] = "new_source_segment_and_additional_price_deterioration"
            if candidate_at is None:
                candidate_at = observed
        else:
            row["candidate"] = "waiting"
            row["reason"] = "additional_deterioration_not_met"
    return {"identity": _owned(ident), "anchor_error": error,
            "frames": output, "frame_denominator": len(output),
            "candidate_counts": dict(Counter(r["candidate"] for r in output)),
            "first_baseline_observed_at": _owned(baseline_at),
            "first_candidate_observed_at": _owned(candidate_at),
            "candidate_minus_baseline_seconds": (
                (candidate_at-baseline_at).total_seconds() if candidate_at and baseline_at else None)}


def build_post_trim_report(cases, calendar, *, as_of, policy):
    """Owned dict/list evidence only; no DB/archives/default policy/calendar reads.

    Naive clocks explicitly denote Asia/Shanghai local exchange time. Callers must
    supply complete calendar rows with registered_at/evidence_ref, not weekdays.
    Frame order is observation order, never reordered by quote source time.
    """
    at = _clock(as_of)
    if at is None or not isinstance(policy, PostTrimPolicy):
        raise ValueError("explicit local as_of and PostTrimPolicy required")
    if type(cases) is not list or type(calendar) is not list:
        raise ValueError("owned lists required")
    registered = defaultdict(list)
    for r in calendar:
        if type(r) is dict and _day(r.get("trade_date")) is not None:
            registered[_day(r["trade_date"])].append(r)
    keys = [_hash({k: c["identity"][k] for k in IDENTITY})
            if type(c) is dict and _identity(c.get("identity")) else None for c in cases]
    counts = Counter(keys)
    results = [_case(c, registered, at, policy, key is not None and counts[key] > 1)
               for c, key in zip(cases, keys)]
    report = _owned({"schema_version": SCHEMA, "policy": asdict(policy), "as_of": at,
                     "case_denominator": len(cases), "results": results,
                     "frame_denominator": sum(r["frame_denominator"] for r in results),
                     "anchor_unknown_case_count": sum(bool(r["anchor_error"]) for r in results),
                     "case_without_frames_count": sum(not r["frames"] for r in results),
                     "candidate_counts": dict(Counter(f["candidate"] for r in results for f in r["frames"])),
                     "baseline_counts": dict(Counter(f["baseline"] for r in results for f in r["frames"])),
                     "input_content_hash": _hash({"cases": cases, "calendar": calendar}),
                     "limitations": [
                         "supplied_owned_evidence_not_independent_authenticity_verification",
                         "observed_state_content_hash_is_not_ledger_or_frozen_revision",
                         "no_orders_execution_permission_or_executable_net_return",
                         "synthetic_tests_do_not_prove_real_forward_samples_available",
                         "research_hypothesis_not_production_parameters_or_profit_optimization",
                         "complete_new_segment_required_after_unknown_gap_or_lunch"]})
    report["report_content_hash"] = _hash(report)
    return report
