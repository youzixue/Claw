"""Frozen-position profit protection ablation. Pure research, never an exit order.

Reuse exit_audit.advance_extrema; do not call the DB/default-fallback position
policy resolver or calendar methods that may fetch data. Input is an explicitly
registered, point-in-time calendar and owned position/quote evidence.
"""
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from types import SimpleNamespace
import hashlib
import json
import math
import re

from app.core.trade_calendar import TRADE_SESSIONS, is_official_closed_day
from app.paper.exit_audit import advance_extrema, finite_profit_pct, position_identity

IDENTITY = ("position_id", "account_id", "account_name", "code", "buy_time",
            "strategy_version", "position_revision")
POSITION_FIELDS = (*IDENTITY, "frozen_at", "buy_price", "buy_amount", "stop_loss_price",
                   "price_basis", "position_ref", "entry_policy", "is_closed")
SAMPLE_FIELDS = (*IDENTITY, "sample_id", "source", "source_quote_at", "received_at", "observed_at",
                 "quote_round_id", "evidence_ref", "price", "price_basis", "basis_ref",
                 "corporate_action_status", "buy_price", "buy_amount", "position_state_at",
                 "sellable_quantity", "quantity_ref", "position_active",
                 "original_exit_trigger", "original_exit_ref")
CALENDAR_FIELDS = ("trade_date", "is_trade_day", "registered_at", "evidence_ref")


def _owned(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if type(value) is dict:
        return {str(k): _owned(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_owned(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return {"invalid_number": "nan" if math.isnan(value) else "+inf" if value > 0 else "-inf"}
    if value is None or type(value) in (str, int, float, bool):
        return value
    return {"invalid_type": type(value).__name__}


def _hash(value):
    return hashlib.sha256(json.dumps(_owned(value), sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _leaves(value, fields):
    if type(value) is not dict:
        return {"invalid_type": type(value).__name__}
    return {k: _owned(value[k]) for k in fields if k in value}


def _ref(value):
    return type(value) is str and bool(value.strip())


def _num(value):
    if type(value) not in (float, int):
        return None
    try:
        return float(value) if math.isfinite(value) else None
    except (OverflowError, ValueError):
        return None


def _threshold_reached(value, threshold):
    """Relative rounding tolerance must never turn a positive gate into zero."""
    return (_num(value) is not None and value > 0
            and (value >= threshold or math.isclose(value, threshold, rel_tol=1e-12, abs_tol=0.0)))


def _clock(value):
    if type(value) is str:
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    return value if isinstance(value, datetime) and value.tzinfo is None else None


def _day(value):
    if type(value) is date:
        return value
    if type(value) is str:
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    return None


def _session(at):
    for name in ("morning", "afternoon"):
        start, end = TRADE_SESSIONS[name]
        if start <= at.time() < end:
            return name
    return None


def _int(value, minimum=0):
    return type(value) is int and value >= minimum


@dataclass(frozen=True)
class ProfitProtectionPolicy:
    version: str
    activation_profit_pct: float
    pullback_from_peak_pct: float
    max_quote_age_sec: int = 90
    max_sample_gap_sec: int = 90

    def __post_init__(self):
        if not _ref(self.version) or not self.version.startswith("research:"):
            raise ValueError("explicit research: identity required")
        for field in ("activation_profit_pct", "pullback_from_peak_pct"):
            value = _num(getattr(self, field))
            if value is None or value <= 0 or value >= 100:
                raise ValueError("research thresholds must be finite in (0,100)")
        if not _int(self.max_quote_age_sec, 1) or not _int(self.max_sample_gap_sec, 1):
            raise ValueError("positive integer quote clocks required")


def _same_identity(row, p):
    return all(type(row.get(k)) is type(p.get(k)) and row.get(k) == p.get(k) for k in IDENTITY)


def _position_error(p, as_of):
    if type(p) is not dict:
        return "position_not_owned_object"
    if (not _int(p.get("position_id"), 1) or not _int(p.get("account_id"), 1)
            or not all(_ref(p.get(k)) for k in ("account_name", "code", "strategy_version",
                "position_revision", "price_basis", "position_ref"))
            or not re.fullmatch("[0-9]{6}", p["code"])):
        return "position_identity_unproven"
    buy, frozen = _clock(p.get("buy_time")), _clock(p.get("frozen_at"))
    if buy is None or frozen is None or not buy <= frozen <= as_of or not _session(buy):
        return "position_clock_invalid"
    if p.get("is_closed") is not False:
        return "position_closed_or_unknown"
    if not _int(p.get("buy_amount"), 1) or any(
            _num(p.get(k)) is None or p[k] <= 0 for k in ("buy_price", "stop_loss_price")):
        return "position_cost_or_quantity_invalid"
    entry = p.get("entry_policy")
    if (type(entry) is not dict or entry.get("basis") != "frozen_entry_order"
            or entry.get("missing_keys") != [] or not _same_identity(entry, p)
            or not _ref(entry.get("order_id")) or not _ref(entry.get("evidence_ref"))
            or not _int(entry.get("first_buy_trade_id"), 1)
            or _clock(entry.get("observed_at")) is None or _clock(entry["observed_at"]) > buy):
        return "complete_frozen_entry_policy_unproven"
    rules = entry.get("exit_parameters")
    if (type(rules) is not dict or not _int(rules.get("max_hold_days"), 1)
            or any(_num(rules.get(k)) is None or rules[k] <= 0 for k in ("stop_loss_pct", "take_profit_pct"))):
        return "frozen_sl_tp_maxhold_incomplete"
    return ""


def _calendar_day(day, observed, registered):
    rows = registered.get(day, [])
    visible = [r for r in rows if _clock(r.get("registered_at")) is not None
               and _clock(r["registered_at"]) <= observed]
    if len(visible) != 1:
        return None
    r = visible[0]
    if not _ref(r.get("evidence_ref")) or type(r.get("is_trade_day")) is not bool:
        return None
    if r["is_trade_day"] and (is_official_closed_day(day) or day.weekday() >= 5):
        return None  # override can reject a false registration, never fill a missing row
    return r["is_trade_day"]


def _sell_date(buy, current, observed, registered):
    if _calendar_day(buy, observed, registered) is not True:
        return None, "buy_calendar_unproven"
    first = None
    day = buy + timedelta(days=1)
    while day <= current:
        state = _calendar_day(day, observed, registered)
        if state is None:
            return None, "calendar_span_incomplete"
        if state and first is None:
            first = day
        day += timedelta(days=1)
    return first, ""


def _gap(previous, current, limit, registered, observed):
    if previous is None:
        return True
    def elapsed(at):
        seconds = 0
        for name in ("morning", "afternoon"):
            start, end = TRADE_SESSIONS[name]
            a, b = datetime.combine(at.date(), start), datetime.combine(at.date(), end)
            seconds += min(max((at-a).total_seconds(), 0), (b-a).total_seconds())
        return seconds
    if current.date() == previous.date():
        return elapsed(current)-elapsed(previous) > limit
    seconds = 14400-elapsed(previous)+elapsed(current)
    day = previous.date()+timedelta(days=1)
    while day < current.date():
        state = _calendar_day(day, observed, registered)
        if state is None:
            return True
        if state:
            seconds += 14400
        day += timedelta(days=1)
    return seconds > limit


def _evaluate(p, rows, registered, as_of, policy):
    pos = SimpleNamespace(id=p["position_id"], account_id=p["account_id"], code=p["code"],
        buy_time=_clock(p["buy_time"]), buy_price=p["buy_price"], strategy_version=p["strategy_version"])
    owned_identity = position_identity(pos)
    state = {"post_entry": None, "post_sellable": None}
    rejected, observations = [], []
    seen, previous, first_sellable = {}, None, None
    sample_ids, round_ids = {}, {}
    sequence_broken, terminated = False, False
    valid_rows = []
    for raw in rows:
        r = _leaves(raw, SAMPLE_FIELDS)
        at = _clock(raw.get("observed_at")) if type(raw) is dict else None
        if at is not None and at > as_of:
            rejected.append({"reason": "future_observation_excluded", "evidence": r, "raw_hash": _hash(r)})
            continue
        valid_rows.append((at, raw, r))
    valid_rows.sort(key=lambda row: (row[0] is not None, row[0], _hash(row[2])))
    for observed, raw, evidence in valid_rows:
        reason = ""
        if type(raw) is not dict:
            reason = "sample_not_owned_object"
        elif not _same_identity(raw, p):
            reason = "position_identity_conflict"
        else:
            source, received = _clock(raw.get("source_quote_at")), _clock(raw.get("received_at"))
            position_at = _clock(raw.get("position_state_at"))
            if (source is None or received is None or observed is None
                    or not _clock(p["frozen_at"]) <= source <= received <= observed
                    or source.date() != observed.date() or received.date() != source.date()
                    or not _session(source) or _session(source) != _session(observed)
                    or _session(received) != _session(source)
                    or (observed-source).total_seconds() > policy.max_quote_age_sec
                    or position_at is None or not _clock(p["frozen_at"]) <= position_at <= observed
                    or (observed-position_at).total_seconds() > policy.max_quote_age_sec):
                reason = "quote_or_position_clock_invalid"
            elif (raw.get("source") != "tencent" or not all(_ref(raw.get(k)) for k in
                    ("sample_id", "quote_round_id", "evidence_ref", "basis_ref"))):
                reason = "quote_source_unproven"
            elif (_num(raw.get("price")) is None or raw["price"] <= 0
                    or raw.get("price_basis") != p["price_basis"]
                    or raw.get("corporate_action_status") != "none"
                    or _num(raw.get("buy_price")) != p["buy_price"]
                    or type(raw.get("buy_amount")) is not int or raw["buy_amount"] != p["buy_amount"]):
                reason = "position_or_price_basis_changed"
            elif finite_profit_pct(raw["price"], p["buy_price"]) is None:
                # Finite inputs can still overflow the shared extrema calculation.
                # Preserve this rejected frame, but never create a numeric alert from it.
                reason = "derived_profit_nonfinite"
            elif _calendar_day(source.date(), observed, registered) is not True:
                reason = "sample_calendar_unproven"
        if reason:
            rejected.append({"reason": reason, "evidence": evidence, "raw_hash": _hash(evidence)})
            sequence_broken = True
            continue
        if terminated:
            rejected.append({"reason": "position_already_closed", "evidence": evidence, "raw_hash": _hash(evidence)})
            continue
        if raw.get("position_active") is False:
            terminated = True
            continue
        if raw.get("position_active") is not True:
            sequence_broken = True
            rejected.append({"reason": "position_active_unknown", "evidence": evidence, "raw_hash": _hash(evidence)})
            continue
        fingerprint = _hash({k: v for k, v in evidence.items() if k not in
                            ("sample_id", "received_at", "observed_at", "evidence_ref")})
        if any(key in registry and registry[key] != source for registry, key in (
                (sample_ids, raw["sample_id"]), (round_ids, raw["quote_round_id"]))):
            sequence_broken = True
            rejected.append({"reason": "sample_or_round_identity_reused", "evidence": evidence, "raw_hash": _hash(evidence)})
            continue
        sample_ids[raw["sample_id"]] = source
        round_ids[raw["quote_round_id"]] = source
        if source in seen:
            if seen[source] != fingerprint:
                sequence_broken = True
                rejected.append({"reason": "conflicting_source_duplicate", "evidence": evidence, "raw_hash": _hash(evidence)})
            continue
        if previous is not None and source < previous:
            sequence_broken = True
            rejected.append({"reason": "source_arrival_order_conflict", "evidence": evidence, "raw_hash": _hash(evidence)})
            continue
        seen[source] = fingerprint
        gap = _gap(previous, source, policy.max_sample_gap_sec, registered, observed)
        sequence_broken = sequence_broken or (gap and previous is not None)
        first_date, calendar_error = _sell_date(pos.buy_time.date(), source.date(), observed, registered)
        quantity = raw.get("sellable_quantity")
        quantity_known = (_int(quantity) and quantity <= p["buy_amount"] and _ref(raw.get("quantity_ref")))
        if calendar_error:
            legal, block = "unknown", calendar_error
        elif source.date() == pos.buy_time.date():
            legal, block = "false", "t_plus_one"  # independent of any alleged sellable quantity
        elif first_date is None:
            legal, block = "unknown", "first_registered_sell_day_unproven"
        elif not quantity_known:
            legal, block = "unknown", "sellable_quantity_unproven"
        elif quantity < 100:
            legal, block = "false", "sellable_quantity_below_existing_100_share_gate"
        else:
            legal, block = "true", ""
        if legal == "true" and first_sellable is None:
            first_sellable = observed
        quote = SimpleNamespace(price=raw["price"], source_quote_at=source,
            received_at=received, quote_round_id=raw["quote_round_id"])
        for basis in state:
            if basis == "post_entry" or (first_sellable is not None and source >= first_sellable):
                state[basis], _ = advance_extrema(state[basis], position=pos, spot=quote,
                    observed_at=observed, quote_ok=True, max_age_sec=policy.max_quote_age_sec)
        row = {"sample_id": raw["sample_id"], "source_quote_at": source, "observed_at": observed,
            "evidence_ref": raw["evidence_ref"], "calendar_first_sell_day": first_date,
            "first_sellable_observation_at": first_sellable,
            "legal_quantity_gate": legal, "execution_block": block,
            "sellable_quantity": quantity if quantity_known else None,
            "continuity": "gap_or_unknown" if sequence_broken or gap else "sampled_segment",
            "original_exit_trigger": "true" if raw.get("original_exit_trigger") is True and _ref(raw.get("original_exit_ref"))
                else "false" if raw.get("original_exit_trigger") is False and _ref(raw.get("original_exit_ref")) else "unknown",
            "original_exit_ref": raw.get("original_exit_ref") if _ref(raw.get("original_exit_ref")) else None,
            "research": {}, "order_generated": False, "executable_net_return": None}
        for basis, extrema in state.items():
            high = extrema.get("post_entry_high") if extrema else None
            peak_profit = finite_profit_pct(high, p["buy_price"])
            pullback = (high-raw["price"])/high*100 if high else None
            crossed = (_threshold_reached(peak_profit, policy.activation_profit_pct)
                       and _threshold_reached(pullback, policy.pullback_from_peak_pct))
            row["research"][basis] = {"observed_high": high,
                "peak_profit_pct": round(peak_profit, 6) if high else None,
                "pullback_from_observed_high_pct": round(pullback, 6) if high else None,
                "trigger": "true" if crossed else "unknown",
                "legal_research_alert": "true" if crossed and legal == "true" and not sequence_broken else "unknown",
                "basis": "observed_prices_not_daily_high", "execution_permission": None}
        observations.append(row)
        previous = source
    return {"policy": {**asdict(policy), "policy_hash": _hash(asdict(policy))},
            "position_identity": owned_identity, "extrema": state,
            "first_sellable_observation_at": first_sellable,
            "observations": observations, "rejected_evidence": rejected,
            "rejection_counts": dict(Counter(r["reason"] for r in rejected)),
            "historical_gap_or_invalid": sequence_broken, "position_terminated": terminated,
            "trigger_is_fill": False, "executable_net_return": None}


def build_profit_protection_report(positions, samples, calendar, *, as_of, policies):
    """Owned frozen mappings only. No rule resolution, calendar sync, restore or writes."""
    as_of = _clock(as_of)
    if as_of is None:
        raise ValueError("explicit naive local as_of required")
    policies = tuple(policies)
    if not policies or any(not isinstance(p, ProfitProtectionPolicy) for p in policies):
        raise ValueError("explicit research policies required")
    if len({p.version for p in policies}) != len(policies):
        raise ValueError("duplicate research policy version")
    positions, samples, calendar = list(positions), list(samples), list(calendar)
    registered = defaultdict(list)
    for row in calendar:
        if type(row) is dict and _day(row.get("trade_date")) is not None:
            registered[_day(row["trade_date"])].append(row)
    grouped = defaultdict(list)
    for row in samples:
        key = row.get("position_id") if type(row) is dict else None
        grouped[key if type(key) is int else None].append(row)
    ids = Counter(p.get("position_id") for p in positions if type(p) is dict and type(p.get("position_id")) is int)
    results = []
    for p in positions:
        error = _position_error(p, as_of)
        key = p.get("position_id") if type(p) is dict and type(p.get("position_id")) is int else None
        if ids.get(key, 0) > 1:
            error = "duplicate_position_identity"
        result = {"frozen_position": _leaves(p, POSITION_FIELDS),
                  "status": "unknown" if error else "evaluated", "reason": error, "comparisons": []}
        if not error:
            result["comparisons"] = [_evaluate(p, grouped[key], registered, as_of, policy) for policy in policies]
        results.append(result)
    report = _owned({"schema_version": "profit_protection_research_v2", "as_of": as_of,
        "read_only": True, "results": results,
        "input_evidence_hash": _hash({"positions": [_leaves(p, POSITION_FIELDS) for p in positions],
            "samples": [_leaves(r, SAMPLE_FIELDS) for r in samples],
            "registered_calendar": [_leaves(r, CALENDAR_FIELDS) for r in calendar]}),
        "unassigned_sample_count": sum(len(rows) for key, rows in grouped.items() if key not in ids),
        "limitations": ["constant_frozen_position_no_add_trim_or_adjustment",
            "registered_calendar_rows_required_no_weekday_fallback",
            "supplied_evidence_not_independent_ledger_authentication",
            "sampled_high_not_tick_extreme", "trigger_not_fill_no_executable_profit",
            "first_sellable_observation_not_proof_of_unobserved_day_open_high",
            "gaps_preserve_observed_extrema_but_block_new_legal_research_alerts",
            "frozen_original_sl_tp_maxhold_not_recomputed_or_modified"]})
    report["data_hash"] = _hash(report)
    return report
