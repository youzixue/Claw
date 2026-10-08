"""Three-arm *reservation* study on frozen, explicitly supplied evidence.

No SQL, current settings, sizing, risk engine, broker, sale or fill simulator is
called here. A selected reservation is NOT an order or an executable return.
The caller must supply original whole-lot requests and non-capacity gate leaves;
the study never removes a failed trigger/ranking/T+1/execution/risk constraint.
"""
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time
from decimal import Decimal, ROUND_CEILING
import hashlib
import json
import math

from app.paper.account_policy import ACCOUNT_NAMES
from app.paper.signal_research import MarkoutPolicy

SCHEMA = "paper_capacity_reservation_study_v1"
ARMS = ("first_come", "fixed_times", "reserve_late")
_GUARDS = ("original_trigger", "current_shape", "data_quality",
           "ranking_contract", "execution_constraints", "risk_excluding_capacity")


def _clock(value, day=None):
    if not isinstance(value, datetime) or value.tzinfo is not None or (day and value.date() != day):
        raise ValueError("a same-day naive Shanghai clock is required")
    return value


def _number(value, *, positive=False, maximum=1e12):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0 or value > maximum:
        raise ValueError("invalid finite nonnegative numeric leaf")
    if positive and value <= 0:
        raise ValueError("positive numeric leaf required")
    return Decimal(str(value))


def _integer(value, *, positive=False):
    if type(value) is not int or value < (1 if positive else 0) or value > 1000000000:
        raise ValueError("invalid integer leaf")


def _sha(value):
    if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise ValueError("evidence SHA256 reference required")


def _code(value):
    if not isinstance(value, str) or len(value) != 6 or not value.isascii() or not value.isdigit():
        raise ValueError("six-digit stock code required")


def _identity(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 160:
        raise ValueError("nonempty bounded identity required")


def _market_time(value):
    return time(9, 30) <= value <= time(11, 30) or time(13) <= value <= time(15)


def _json(value):
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError("not an owned JSON leaf")


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    default=_json, allow_nan=False).encode()).hexdigest()


@dataclass(frozen=True)
class CapacityScope:
    account_id: int
    account_name: str
    strategy_version: str
    trade_date: date
    observed_at: datetime
    window_end: datetime
    available_cash: float
    per_order_cash_limit: float
    max_positions: int
    max_daily_buys: int
    used_daily_buys: int
    encumbered_codes: tuple[str, ...]
    evidence_sha256: str
    registered_trade_day: bool

    def __post_init__(self):
        _integer(self.account_id, positive=True)
        if self.account_name not in ACCOUNT_NAMES or type(self.trade_date) is not date:
            raise ValueError("one exact experiment account and trade date required")
        _identity(self.strategy_version)
        _clock(self.observed_at, self.trade_date)
        _clock(self.window_end, self.trade_date)
        if (self.registered_trade_day is not True or self.observed_at >= self.window_end
                or not _market_time(self.observed_at.time()) or not _market_time(self.window_end.time())):
            raise ValueError("registered trading session and ordered market window required")
        _number(self.available_cash)
        _number(self.per_order_cash_limit, positive=True)
        for key in ("max_positions", "max_daily_buys", "used_daily_buys"):
            _integer(getattr(self, key))
        if type(self.encumbered_codes) is not tuple or len(set(self.encumbered_codes)) != len(self.encumbered_codes):
            raise ValueError("distinct frozen held/pending codes required")
        for code in self.encumbered_codes:
            _code(code)
        _sha(self.evidence_sha256)


@dataclass(frozen=True)
class CapacitySignal:
    signal_id: str
    account_id: int
    account_name: str
    strategy_version: str
    code: str
    confirmed_at: datetime
    expires_at: datetime
    quantity: int
    limit_price: float
    priority_score: float
    priority_at: datetime
    evidence_sha256: str
    entry_regime: str = "unknown"
    entry_bull_bear: str = "unknown"

    def __post_init__(self):
        _identity(self.signal_id)
        _integer(self.account_id, positive=True)
        if self.account_name not in ACCOUNT_NAMES:
            raise ValueError("exact experiment account required")
        _identity(self.strategy_version)
        _code(self.code)
        day = _clock(self.confirmed_at).date()
        _clock(self.expires_at, day)
        _clock(self.priority_at, day)
        if self.priority_at > self.confirmed_at or self.expires_at < self.confirmed_at:
            raise ValueError("priority must be frozen by confirmation; no retrospective expiry")
        if not _market_time(self.confirmed_at.time()):
            raise ValueError("signal must be observed in a regular trading session")
        _integer(self.quantity, positive=True)
        if self.quantity % 100:
            raise ValueError("buy quantity must preserve positive whole A-share lots")
        price = _number(self.limit_price, positive=True)
        if price * 100 != (price * 100).to_integral_value():
            raise ValueError("original limit price must be on the cent tick")
        if type(self.priority_score) not in (int, float) or not math.isfinite(self.priority_score):
            raise ValueError("finite point-in-time priority required")
        _sha(self.evidence_sha256)
        _identity(self.entry_regime)
        _identity(self.entry_bull_bear)


@dataclass(frozen=True)
class NonCapacityGuards:
    original_trigger: bool | None
    current_shape: bool | None
    data_quality: bool | None
    ranking_contract: bool | None
    execution_constraints: bool | None
    risk_excluding_capacity: bool | None

    def __post_init__(self):
        if any(getattr(self, key) is not None and type(getattr(self, key)) is not bool for key in _GUARDS):
            raise ValueError("guard leaves must be true, false or unknown, not truthy values")

    def rejection(self):
        blocked = [key for key in _GUARDS if getattr(self, key) is False]
        unknown = [key for key in _GUARDS if getattr(self, key) is None]
        if blocked:
            return "non_capacity_blocked:" + ",".join(blocked)
        if unknown:
            return "non_capacity_unknown:" + ",".join(unknown)
        return None


@dataclass(frozen=True)
class CapacityObservation:
    signal_id: str
    account_id: int
    account_name: str
    strategy_version: str
    code: str
    source_at: datetime
    received_at: datetime
    observed_at: datetime
    guard_at: datetime
    reference_price: float
    quote_round_id: str
    guards: NonCapacityGuards
    evidence_sha256: str

    def __post_init__(self):
        _identity(self.signal_id)
        _integer(self.account_id, positive=True)
        if self.account_name not in ACCOUNT_NAMES:
            raise ValueError("exact experiment account required")
        _identity(self.strategy_version)
        _code(self.code)
        day = _clock(self.observed_at).date()
        for key in ("source_at", "received_at", "guard_at"):
            _clock(getattr(self, key), day)
        if not self.source_at <= self.received_at <= self.guard_at <= self.observed_at:
            raise ValueError("source/receipt/guard/availability clocks must be causal")
        if not _market_time(self.source_at.time()) or not _market_time(self.observed_at.time()):
            raise ValueError("quote/guard observation outside regular market session")
        _number(self.reference_price, positive=True)
        _identity(self.quote_round_id)
        if type(self.guards) is not NonCapacityGuards:
            raise ValueError("frozen non-capacity guard contract required")
        _sha(self.evidence_sha256)


@dataclass(frozen=True)
class CapacityPolicy:
    fixed_times: tuple[time, ...] = (time(10), time(13, 30))
    reserve_until: time = time(13)
    reserve_cash_fraction: float = 0.40
    reserve_slots: int = 1
    max_quote_age_sec: int = 90
    max_guard_age_sec: int = 90
    costs: MarkoutPolicy = field(default_factory=MarkoutPolicy)

    def __post_init__(self):
        if (type(self.fixed_times) is not tuple or not self.fixed_times
                or any(type(t) is not time or t.tzinfo is not None or not _market_time(t) for t in self.fixed_times)
                or tuple(sorted(set(self.fixed_times))) != self.fixed_times):
            raise ValueError("distinct ordered fixed market times required")
        if type(self.reserve_until) is not time or self.reserve_until.tzinfo is not None or not _market_time(self.reserve_until):
            raise ValueError("reserve release must be a regular market time")
        _number(self.reserve_cash_fraction, maximum=1)
        _integer(self.reserve_slots)
        _integer(self.max_quote_age_sec, positive=True)
        _integer(self.max_guard_age_sec, positive=True)
        if type(self.costs) is not MarkoutPolicy:
            raise ValueError("explicit shared cost assumption required")

    def contract(self):
        raw = asdict(self)
        return {"version": SCHEMA + "-" + _digest(raw)[:16], **raw,
                "costs_scope": "buy_limit_notional_plus_commission_ceiling_reservation_only",
                "cost_quantity": "original_signal_quantity_not_markout_default",
                "priority": "frozen_confirmation_priority_fixed_arm_only",
                "sell_proceeds_recycled": False}


def _reservation(signal, policy):
    amount = Decimal(str(signal.limit_price)) * signal.quantity
    # Reserve a conservative cent ceiling; this is not an actual charged fee.
    commission = max(Decimal(str(policy.costs.minimum_commission)),
                     amount * Decimal(str(policy.costs.commission_rate))).quantize(
                         Decimal("0.01"), rounding=ROUND_CEILING)
    return amount + commission


def run_capacity_study(scope, signals, observations, *, as_of, policy=None):
    """Replay one frozen account-instance/version/day. All arms see the same data.

    Input declarations are validated, not independently authenticated here; archive
    readers must verify the referenced SHA/identity before constructing inputs.
    No current PaperAccount/StockTag or outcome prices are read to repair gaps.
    """
    if type(scope) is not CapacityScope:
        raise ValueError("frozen capacity scope required")
    _clock(as_of)
    if as_of < scope.observed_at:
        raise ValueError("as_of precedes initial capacity snapshot")
    policy = policy or CapacityPolicy()
    if type(policy) is not CapacityPolicy:
        raise ValueError("capacity research policy required")
    cutoff = min(as_of, scope.window_end)
    signals, observations = tuple(signals), tuple(observations)
    if len(signals) > 10000 or len(observations) > 200000:
        raise ValueError("research input bound exceeded; partition by account/version/day")
    if any(type(s) is not CapacitySignal for s in signals) or any(type(o) is not CapacityObservation for o in observations):
        raise ValueError("only frozen owned research records accepted")
    # Unavailable future leaves cannot alter a prior decision or its input hash.
    visible = sorted((s for s in signals if s.confirmed_at <= cutoff),
                     key=lambda s: (s.confirmed_at, s.code, s.signal_id))
    rows = sorted((o for o in observations if o.observed_at <= cutoff),
                  key=lambda o: (o.observed_at, o.signal_id, o.quote_round_id))
    by_id = {}
    for s in visible:
        if s.signal_id in by_id:
            raise ValueError("duplicate signal identity, do not silently improve denominator")
        if (s.account_id, s.account_name, s.strategy_version) != (scope.account_id, scope.account_name, scope.strategy_version):
            raise ValueError("signal account instance/version mismatch")
        if s.confirmed_at.date() != scope.trade_date or s.confirmed_at < scope.observed_at:
            raise ValueError("signal outside frozen account day/window")
        by_id[s.signal_id] = s
    seen_rows, source_prices, guard_states = set(), {}, {}
    for o in rows:
        s = by_id.get(o.signal_id)
        if s is None or o.code != s.code or o.observed_at < s.confirmed_at or o.guard_at < s.confirmed_at:
            raise ValueError("quote identity or pre-confirmation clock mismatch")
        if (o.account_id, o.account_name, o.strategy_version) != (s.account_id, s.account_name, s.strategy_version):
            raise ValueError("observation account instance/version mismatch")
        key = (o.signal_id, o.observed_at)
        if key in seen_rows:
            raise ValueError("duplicate decision observation; do not choose a favorable copy")
        seen_rows.add(key)
        source_key = (o.code, o.source_at)
        if source_key in source_prices and source_prices[source_key] != o.reference_price:
            raise ValueError("conflicting price at the same source clock")
        source_prices[source_key] = o.reference_price
        guard_key = (o.signal_id, o.guard_at)
        if guard_key in guard_states and guard_states[guard_key] != o.guards:
            raise ValueError("conflicting guards at the same decision clock")
        guard_states[guard_key] = o.guards
    first_by_code, duplicates = {}, set()
    for s in visible:
        if s.code in first_by_code:
            duplicates.add(s.signal_id)
        else:
            first_by_code[s.code] = s.signal_id
    fixed = {datetime.combine(scope.trade_date, t) for t in policy.fixed_times}
    release = datetime.combine(scope.trade_date, policy.reserve_until)
    initial_cash = Decimal(str(scope.available_cash))
    slot_limit = max(0, min(scope.max_positions - len(scope.encumbered_codes),
                            scope.max_daily_buys - scope.used_daily_buys))
    inputs = {"scope": asdict(scope), "signals": [asdict(s) for s in visible],
              "observations": [asdict(o) for o in rows], "cutoff": cutoff}
    output = {}
    for arm in ARMS:
        by_time = {}
        for o in rows:
            by_time.setdefault(o.observed_at, []).append(o)
        times = set(by_time)
        if arm == "fixed_times":
            times |= fixed
        if arm == "reserve_late":
            times.add(release)
        times = sorted(t for t in times if scope.observed_at <= t <= cutoff)
        latest, selected, decisions = {}, {}, []
        cash = initial_cash
        states = {s.signal_id: "awaiting_observation" for s in visible}
        for at in times:
            for o in by_time.get(at, ()):
                prior = latest.get(o.signal_id)
                # A late older quote must not reinstate a newer invalidated state.
                if prior is not None and (o.source_at < prior.source_at or o.guard_at < prior.guard_at):
                    raise ValueError("out-of-order source/guard evidence requires upstream quarantine")
                latest[o.signal_id] = o
            if arm == "fixed_times" and at not in fixed:
                continue
            # Cash and available slots only decrease; a rejected reservation can
            # become eligible again only with new evidence or the explicit release.
            # Do not rescan the whole candidate universe for every unrelated quote.
            reevaluate_all = arm == "fixed_times" or (arm == "reserve_late" and at == release)
            candidates_now = visible if reevaluate_all else [
                by_id[o.signal_id] for o in by_time.get(at, ())]
            pending = [s for s in candidates_now if s.confirmed_at <= at and s.signal_id not in selected]
            pending.sort(key=(lambda s: (-s.priority_score, s.confirmed_at, s.code, s.signal_id))
                         if arm == "fixed_times" else
                         (lambda s: (s.confirmed_at, s.code, s.signal_id)))
            for s in pending:
                reason = None
                o = latest.get(s.signal_id)
                if s.signal_id in duplicates:
                    reason = "repeated_same_day_code_not_independent_sample"
                elif s.code in scope.encumbered_codes:
                    reason = "held_or_pending_code_no_add_on"
                elif at > s.expires_at:
                    reason = "original_signal_expired"
                elif o is None:
                    reason = "decision_observation_missing"
                elif (at - o.source_at).total_seconds() > policy.max_quote_age_sec:
                    reason = "quote_stale_at_treatment_time"
                elif (at - o.guard_at).total_seconds() > policy.max_guard_age_sec:
                    reason = "guards_stale_at_treatment_time"
                elif o.guards.rejection():
                    reason = o.guards.rejection()
                elif Decimal(str(o.reference_price)) * (1 + Decimal(str(policy.costs.slippage_pct)) / 100) > Decimal(str(s.limit_price)):
                    reason = "original_limit_price_exceeded"
                cost = _reservation(s, policy)
                if reason is None and cost > Decimal(str(scope.per_order_cash_limit)):
                    reason = "original_per_order_budget_exceeded"
                elif reason is None and len(selected) >= slot_limit:
                    reason = "original_position_or_daily_capacity_full"
                elif reason is None and cost > cash:
                    reason = "original_available_cash_insufficient"
                if reason is None and arm == "reserve_late" and at < release:
                    reserve_floor = initial_cash * Decimal(str(policy.reserve_cash_fraction))
                    if (cash - cost < reserve_floor
                            or len(selected) >= max(0, slot_limit - policy.reserve_slots)):
                        reason = "reserved_for_late_session"
                status = "selected_reservation_only" if reason is None else reason
                if states[s.signal_id] != status:
                    decisions.append({"signal_id": s.signal_id, "at": at.isoformat(),
                                      "status": status, "quote_round_id": o.quote_round_id if o else None})
                states[s.signal_id] = status
                if reason is None:
                    cash -= cost
                    selected[s.signal_id] = {
                        "signal_id": s.signal_id, "code": s.code, "selected_at": at.isoformat(),
                        "quantity": s.quantity, "limit_price": s.limit_price,
                        "reserved_cash": float(cost), "priority_score": s.priority_score,
                        "quote_round_id": o.quote_round_id, "source_at": o.source_at.isoformat(),
                        "guard_at": o.guard_at.isoformat(), "entry_regime": s.entry_regime,
                        "entry_bull_bear": s.entry_bull_bear, "fill_status": "unverified",
                        "actual_fill_quantity": None, "executable_net_pnl": None,
                    }
        for s in visible:
            if s.signal_id in selected:
                continue
            if s.signal_id in duplicates:
                states[s.signal_id] = "repeated_same_day_code_not_independent_sample"
            elif s.code in scope.encumbered_codes:
                states[s.signal_id] = "held_or_pending_code_no_add_on"
            elif cutoff > s.expires_at:
                states[s.signal_id] = "original_signal_expired"
            elif arm == "fixed_times" and not any(scope.observed_at <= t <= cutoff and t >= s.confirmed_at for t in fixed):
                states[s.signal_id] = "fixed_treatment_not_elapsed"
        output[arm] = {"selected": list(selected.values()), "selected_count": len(selected),
                       "unreserved_cash": float(cash), "reserved_cash": float(initial_cash - cash),
                       "remaining_new_slots": max(0, slot_limit - len(selected)),
                       "terminal_states": states, "state_counts": dict(Counter(states.values())),
                       "state_basis": "last_treatment_attempt_plus_known_expiry_not_live_permission",
                       "decisions": decisions, "executable_net_pnl": None}
    result = {"schema": SCHEMA, "as_of": as_of.isoformat(), "cutoff": cutoff.isoformat(),
              "scope": asdict(scope), "policy": policy.contract(),
              "input_sha256": _digest(inputs), "signal_count": len(visible),
              "first_account_version_day_code_count": len(first_by_code),
              "arms": output, "winner": None, "performance_difference": None,
              "contract": {
                  "mode": "offline_capacity_reservations_not_executable_portfolio",
                  "session_scope": "regular_session_no_auction_order_simulation",
                  "input_evidence": "validated_declared_leaves_archive_authentication_required_by_caller",
                  "same_initial_budget_and_non_capacity_guards": True,
                  "cash_basis": "frozen_free_cash_after_existing_positions_pending_reservations_and_risk_buffers",
                  "sizing": "original_request_preserved_no_lot_increase_no_partial_resize",
                  "filled_or_recycled_cash": False, "sell_or_same_day_t1_reuse": False,
                  "pending_cancel_and_queue_fill_simulated": False,
                  "ranking": "capacity_timing_only_never_overrides_original_ranking_contract",
                  "production_policy_changed": False,
                  "counterfactual_nav_risk_recomputed": False,
                  "guard_scope": "frozen_declared_non_capacity_guards_not_counterfactual_portfolio_risk",
                  "same_shape_market_denominator_available": False,
                  "cost_and_t1_t3_performance": "requires_separate_frozen_matching_and_outcome_evidence",
              }}
    # Convert only our own dataclass leaves, never live services/ORM objects.
    result = json.loads(json.dumps(result, default=_json, allow_nan=False))
    result["report_sha256"] = _digest(result)
    return result


@dataclass(frozen=True)
class SharedCapacityBudget:
    """Frozen free capital for an offline overlay, not twelve account balances."""
    trade_date: date
    observed_at: datetime
    window_end: datetime
    available_cash: float
    max_positions: int
    max_daily_buys: int
    used_daily_buys: int
    encumbered_codes: tuple[str, ...]
    evidence_sha256: str

    def __post_init__(self):
        if type(self.trade_date) is not date:
            raise ValueError("one shared trade date required")
        _clock(self.observed_at, self.trade_date)
        _clock(self.window_end, self.trade_date)
        if self.observed_at >= self.window_end:
            raise ValueError("ordered shared budget window required")
        _number(self.available_cash)
        for key in ("max_positions", "max_daily_buys", "used_daily_buys"):
            _integer(getattr(self, key))
        if type(self.encumbered_codes) is not tuple or len(set(self.encumbered_codes)) != len(self.encumbered_codes):
            raise ValueError("distinct frozen shared held/pending codes required")
        for code in self.encumbered_codes:
            _code(code)
        _sha(self.evidence_sha256)


def run_shared_capacity_study(budget, scopes, signals, observations, *, as_of, policy=None):
    """Overlay one capital cap on the existing per-account reservation arms.

    Preserve each arm's independent requests, including excluded/failed signals.
    Do not retroactively refill a route slot freed by a shared rejection; that
    requires a full counterfactual risk/account replay and is NOT simulated here.
    Cross-route priority scores are not calibrated and must not be compared.
    """
    if type(budget) is not SharedCapacityBudget:
        raise ValueError("frozen shared budget required")
    scopes, signals, observations = tuple(scopes), tuple(signals), tuple(observations)
    if not scopes or len(scopes) > len(ACCOUNT_NAMES):
        raise ValueError("one to twelve explicit account scopes required")
    _clock(as_of)
    if as_of < budget.observed_at:
        raise ValueError("as_of precedes shared budget")
    key = lambda item: (item.account_id, item.account_name, item.strategy_version)
    scope_map = {}
    for scope in scopes:
        if type(scope) is not CapacityScope:
            raise ValueError("existing capacity scope required")
        if (scope.trade_date, scope.observed_at, scope.window_end) != (
                budget.trade_date, budget.observed_at, budget.window_end):
            raise ValueError("all arms require the same frozen day/window")
        if key(scope) in scope_map or any(
                prior.account_id == scope.account_id or prior.account_name == scope.account_name
                for prior in scope_map.values()):
            raise ValueError("duplicate account/version scope")
        scope_map[key(scope)] = scope
    if any(type(s) is not CapacitySignal for s in signals) or any(type(o) is not CapacityObservation for o in observations):
        raise ValueError("existing owned signal/observation records required")
    cutoff = min(as_of, budget.window_end)
    visible_signals = tuple(s for s in signals if s.confirmed_at <= cutoff)
    visible_observations = tuple(o for o in observations if o.observed_at <= cutoff)
    if any(key(item) not in scope_map for item in (*visible_signals, *visible_observations)):
        raise ValueError("unmapped account must not disappear from denominator")
    reports = []
    for identity, scope in sorted(scope_map.items()):
        report = run_capacity_study(scope,
            [s for s in visible_signals if key(s) == identity],
            [o for o in visible_observations if key(o) == identity], as_of=as_of, policy=policy)
        reports.append(report)
    arms = {}
    initial = Decimal(str(budget.available_cash))
    slots = max(0, min(budget.max_positions-len(budget.encumbered_codes),
                       budget.max_daily_buys-budget.used_daily_buys))
    for arm in ARMS:
        requests, excluded, all_states = [], [], []
        for report in reports:
            scope = report["scope"]
            identity = {k: scope[k] for k in ("account_id", "account_name", "strategy_version")}
            selected_ids = {r["signal_id"] for r in report["arms"][arm]["selected"]}
            for selected in report["arms"][arm]["selected"]:
                requests.append({**identity, **selected})
            for signal_id, state in report["arms"][arm]["terminal_states"].items():
                all_states.append({**identity, "signal_id": signal_id, "independent_state": state})
                if signal_id not in selected_ids:
                    excluded.append({**identity, "signal_id": signal_id, "status": state,
                                     "stage": "independent_noncapacity_or_capacity"})
        # Deterministic technical tie-break only, not a claim of superior alpha.
        requests.sort(key=lambda r: (r["selected_at"], r["account_name"], r["code"], r["signal_id"]))
        cash, held, selected, rejected = initial, set(budget.encumbered_codes), [], []
        for request in requests:
            cost = Decimal(str(request["reserved_cash"]))
            reason = ("shared_held_or_reserved_code" if request["code"] in held else
                      "shared_position_or_daily_capacity_full" if len(selected) >= slots else
                      "shared_available_cash_insufficient" if cost > cash else None)
            if reason:
                rejected.append({**request, "status": reason, "stage": "shared_budget_overlay"})
            else:
                cash -= cost
                held.add(request["code"])
                selected.append({**request, "status": "selected_reservation_only"})
        arms[arm] = {
            "selected": selected, "displaced": rejected, "independent_excluded": excluded,
            "all_signal_states": all_states, "signal_count": len(all_states),
            "selected_count": len(selected), "displaced_count": len(rejected),
            "reserved_cash": float(initial-cash), "unreserved_cash": float(cash),
            "by_account_selected": dict(Counter(r["account_name"] for r in selected)),
            "remaining_new_slots": max(0, slots-len(selected)), "executable_net_pnl": None,
        }
        assert len(selected)+len(rejected)+len(excluded) == len(all_states)
    result = {
        "schema": "shared_capacity_overlay_v1", "as_of": as_of.isoformat(),
        "budget": asdict(budget), "policy": reports[0]["policy"], "arms": arms,
        "signal_count": sum(report["signal_count"] for report in reports),
        "independent_reports": reports, "winner": None, "performance_difference": None,
        "contract": {
            "mode": "offline_shared_reservations_not_executable_portfolio",
            "same_shared_cash_across_arms": True, "production_policy_changed": False,
            "cross_strategy_priority_comparison": False,
            "tie_break": "selection_clock_account_name_code_signal_id_not_alpha",
            "displaced_and_failed_signals_retained": True,
            "backfill_or_resize_or_cash_recycling": False,
            "counterfactual_portfolio_risk_or_fill_recomputed": False,
            "initial_cash_basis": "explicit_free_cash_after_frozen_holds_and_pending_reservations",
            "input_evidence": "declared_frozen_evidence_requires_caller_authentication",
            "promotable_to_production": False,
        },
    }
    result = json.loads(json.dumps(result, default=_json, allow_nan=False))
    result["report_sha256"] = _digest(result)
    return result
