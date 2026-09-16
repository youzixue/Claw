"""Frozen-run route allocation research. No training, ledger writes or orders.

Observed production lists are preserved, not reconstructed. A probability-only
global control isolates allocation from the production ranker's other criteria.
Only the immutable ledger is read; realized outcomes never enter selection.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.models.promotion import PromotionPredictionRun, PromotionPredictionSnapshot
from app.promotion.route_contract import persisted_route_gate

RESEARCH_VERSION = "promotion_route_rank_research_v2_quality_contract"
C_ROUTE = "mainline_spread_start"
CONTEXTS = {
    "promotion_1510", "promotion_2000", "promotion_0925", "promotion_0935",
    "promotion_1000", "promotion_1030", "promotion_1305",
}


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, ensure_ascii=False, default=str,
        separators=(",", ":"), allow_nan=False,
    ).encode()).hexdigest()


def _object(value: str | None) -> dict:
    try:
        parsed = json.loads(value or "{}")
    except (ValueError, TypeError) as exc:
        raise ValueError("invalid frozen JSON") from exc
    if not isinstance(parsed, dict):
        raise ValueError("frozen JSON must be an object")
    return parsed


def _local_clock(value: datetime | None) -> bool:
    return isinstance(value, datetime) and value.tzinfo is None


@dataclass(frozen=True)
class RouteQuotaContract:
    """Reserved minima, not hard route caps; recall quota includes formal slots."""

    formal_quota: int
    recall_quota: int
    route: str = C_ROUTE
    version: str = RESEARCH_VERSION

    def __post_init__(self):
        if self.version != RESEARCH_VERSION or self.route != C_ROUTE:
            raise ValueError("unsupported research version or route")
        if any(type(v) is not int or v < 0 for v in (self.formal_quota, self.recall_quota)):
            raise ValueError("quotas must be nonnegative integers")
        if self.recall_quota < self.formal_quota:
            raise ValueError("recall quota includes and must cover formal quota")


def _reserve(ordered: list[dict], capacity: int, quota: int, route: str,
             prefix: list[dict] | None = None) -> list[dict]:
    selected = list(prefix or [])
    selected_ids = {r["id"] for r in selected}
    needed = max(0, quota - sum(r["route"] == route for r in selected))
    reserved = [r for r in ordered if r["route"] == route and r["id"] not in selected_ids][:needed]
    # Reserved names compete by the identical frozen score, then unused slots
    # return to the global order. No quota can increase the fixed total capacity.
    selected.extend(reserved[:max(0, capacity - len(selected))])
    selected_ids = {r["id"] for r in selected}
    selected.extend([r for r in ordered if r["id"] not in selected_ids][:max(0, capacity - len(selected))])
    return selected


def compare_frozen_run(
    run: PromotionPredictionRun,
    snapshots: list[PromotionPredictionSnapshot],
    *,
    decision_at: datetime,
    contract: RouteQuotaContract,
    quote_as_of: datetime | None = None,
) -> dict:
    """Pure comparison on the FULL run. Invalid clocks/contracts fail closed.

    Explicit run identity is intentional: this offline entry never falls back to
    an older run. Supplying an old run is retrospective research, not a claim
    that production would have selected that run at the requested decision time.
    """
    if not _local_clock(decision_at):
        raise ValueError("decision_at must be explicit naive Asia/Shanghai time")
    if quote_as_of is not None and (
        not _local_clock(quote_as_of) or quote_as_of.date() != decision_at.date()
    ):
        raise ValueError("invalid quote_as_of clock")
    cutoff = min(decision_at, quote_as_of) if quote_as_of is not None else decision_at
    clocks = (run.as_of_at, run.created_at, run.completed_at)
    if any(not _local_clock(t) for t in clocks):
        raise ValueError("run missing verifiable clocks")
    if not (clocks[0] <= clocks[1] <= clocks[2] <= cutoff):
        raise ValueError("run future or reversed clocks; no fallback")
    if run.status != "completed" or run.snapshot_source != "schedule" or run.snapshot_context not in CONTEXTS:
        raise ValueError("requires completed official supported-context run")
    if run.reference_trade_date != run.as_of_at.date():
        raise ValueError("run reference day does not match as-of day")
    if len(snapshots) != run.candidate_count:
        raise ValueError("incomplete full-run denominator")
    if any(s.run_id != run.id for s in snapshots):
        raise ValueError("mixed runs")
    if (any(type(s.id) is not int or s.id <= 0 for s in snapshots)
            or len({s.id for s in snapshots}) != len(snapshots)):
        raise ValueError("missing/duplicate snapshot IDs")
    if any(not _local_clock(s.created_at) or not (
        run.as_of_at <= s.created_at <= run.completed_at
    ) for s in snapshots):
        raise ValueError("snapshot missing/future/reversed clock")
    if len({s.record_key for s in snapshots}) != len(snapshots):
        raise ValueError("duplicate snapshot identity")
    lane = [s for s in snapshots if s.target_board == 1]
    rows, rank_contracts = [], set()
    for s in lane:
        f = _object(s.features_json)
        if f.get("prediction_rank_contract_complete") is not True or f.get(
            "prediction_rank_contract_version"
        ) != "promotion_rank_contract_v1" or type(f.get("prediction_rank_eligible")) is not bool:
            raise ValueError("incomplete/unsupported frozen rank contract")
        if s.prediction_trade_date != run.reference_trade_date:
            raise ValueError("mixed prediction dates")
        limits = (f.get("prediction_ranked_limit"), f.get("prediction_recall_ranked_limit"))
        if any(type(v) is not int or v <= 0 for v in limits) or limits[1] < limits[0]:
            raise ValueError("invalid frozen formal/recall capacities")
        rank_contracts.add(limits)
        if s.rank_scope not in {"ranked", "recall_ranked", "pool_unranked"}:
            raise ValueError("unknown frozen rank scope")
        formal = s.rank_scope == "ranked"
        recall = s.rank_scope in {"ranked", "recall_ranked"}
        if formal and (not s.rank_position or s.rank_position < 1):
            raise ValueError("formal rank position missing")
        if recall and (not s.recall_rank_position or s.recall_rank_position < 1):
            raise ValueError("recall rank position missing")
        eligible = f["prediction_rank_eligible"]
        if recall and not eligible:
            raise ValueError("observed rank outside common eligibility")
        probability = s.calibrated_probability
        if eligible and (isinstance(probability, bool) or not isinstance(probability, (float, int))
                         or not math.isfinite(probability) or not 0 <= probability <= 1):
            raise ValueError("eligible frozen probability missing/invalid")
        rows.append({
            "id": s.id, "record_key": s.record_key, "code": s.code,
            "route": s.candidate_route, "eligible": eligible,
            "probability": probability if eligible else None,
            "formal": formal, "recall": recall,
            "formal_position": s.rank_position, "recall_position": s.recall_rank_position,
            "rank_scope": s.rank_scope, "trade_gate_passed": s.trade_gate_passed is True,
            "watch_only": s.watch_only is True, "actionable": s.actionable is True,
        })
    if len({r["code"] for r in rows}) != len(rows):
        raise ValueError("duplicate stock in first-board denominator")
    if len(rank_contracts) > 1:
        raise ValueError("mixed frozen capacities")
    # Empty full runs remain valid diagnostics, but cannot invent frozen capacity.
    formal_limit, recall_limit = next(iter(rank_contracts), (0, 0))
    if contract.formal_quota > formal_limit or contract.recall_quota > recall_limit:
        raise ValueError("quota exceeds frozen capacity (or empty run)")
    eligible = sorted([r for r in rows if r["eligible"]],
                      key=lambda r: (-r["probability"], r["code"], r["id"]))
    observed_formal = sorted([r for r in rows if r["formal"]], key=lambda r: r["formal_position"])
    observed_recall = sorted([r for r in rows if r["recall"]], key=lambda r: r["recall_position"])
    for selected, key, capacity in (
        (observed_formal, "formal_position", formal_limit),
        (observed_recall, "recall_position", recall_limit),
    ):
        positions = [r[key] for r in selected]
        if len(positions) != len(set(positions)) or any(p > capacity for p in positions):
            raise ValueError("duplicate/out-of-capacity frozen positions")
    if [r["id"] for r in observed_recall[:len(observed_formal)]] != [r["id"] for r in observed_formal]:
        raise ValueError("formal list is not a frozen recall prefix")
    quota_formal = _reserve(eligible, formal_limit, contract.formal_quota, contract.route)
    quota_recall = _reserve(eligible, recall_limit, contract.recall_quota, contract.route, quota_formal)
    metadata = _object(run.metadata_json)
    quality = metadata.get("quality_gate")
    quality = quality if isinstance(quality, dict) else {}
    route_gate = persisted_route_gate(quality, contract.route)
    # Historical gate_passed can be optimistic. Preserve it but never interpret
    # stale/missing funding as current execution evidence.
    funding = [w for w in quality.get("watermarks", [])
               if isinstance(w, dict) and w.get("dataset") == "fund_flow"]
    funding_current = bool(funding) and all(
        w.get("trade_date") == decision_at.date().isoformat() and w.get("status") == "ok"
        and (w.get("record_count") or 0) > 0 for w in funding
    )
    c_rows = [r for r in rows if r["route"] == contract.route]
    c_order = [r for r in eligible if r["route"] == contract.route]

    def arm(formal: list[dict], recall: list[dict]) -> dict:
        return {
            "denominator": len(eligible),
            "formal_ids": [r["id"] for r in formal],
            "recall_including_formal_ids": [r["id"] for r in recall],
            "formal_count": len(formal), "recall_count": len(recall),
            "formal_route_counts": dict(sorted(Counter(r["route"] for r in formal).items())),
            "recall_route_counts": dict(sorted(Counter(r["route"] for r in recall).items())),
            "cost_after_return": None, "precision": None, "outcome_recall": None,
            "outcome_status": "unknown_not_loaded",
        }

    configuration = {
        **asdict(contract), "target_board": 1,
        "formal_capacity": formal_limit, "recall_capacity": recall_limit,
        "eligibility": "frozen prediction_rank_eligible exactly true; no trade filter",
        "score": "frozen calibrated_probability descending; code/id ascending ties",
        "quota_semantics": "reserved minimum; unused capacity global-fill; recall includes formal prefix",
        "denominator": "all eligible first-board rows from one complete immutable run",
        "observed_baseline": "exact frozen production formal and recall membership/order",
        "global_control": "probability-only; not a reproduction of production ranking",
        "causal_boundary": "quota vs global_control isolates allocation; vs production also changes ranking",
        "clock_timezone": "Asia/Shanghai naive",
    }
    evidence = {
        "run_id": run.id, "run_key": run.run_key, "payload_hash": run.payload_hash,
        "model_version": run.model_version, "feature_version": run.feature_version,
        "data_version": run.data_version, "snapshot_context": run.snapshot_context,
        "reference_trade_date": run.reference_trade_date.isoformat(),
        "as_of_at": run.as_of_at.isoformat(), "created_at": run.created_at.isoformat(),
        "completed_at": run.completed_at.isoformat(), "decision_at": decision_at.isoformat(),
        "quote_as_of": quote_as_of.isoformat() if quote_as_of else None,
        "visible_cutoff": cutoff.isoformat(), "full_run_rows": len(snapshots),
        "first_board_rows": len(rows), "eligible_rows": len(eligible),
        "input_hash": _digest(sorted(rows, key=lambda r: r["id"])),
    }
    return {
        "status": "retrospective_readonly_research",
        "research_version": RESEARCH_VERSION,
        "contract": configuration, "contract_hash": _digest(configuration),
        "evidence": evidence,
        "comparison_hash": _digest({"contract": configuration, "evidence": evidence}),
        "arms": {
            "production_global_frozen": arm(observed_formal, observed_recall),
            "global_probability_control": arm(eligible[:formal_limit], eligible[:recall_limit]),
            "routequota_probability_hypothesis": arm(quota_formal, quota_recall),
        },
        "route_funnel": {
            "pool": len(c_rows), "rank_eligible": len(c_order),
            "trade_gate_nonwatch": sum(r["trade_gate_passed"] and not r["watch_only"] for r in c_rows),
            "formal_or_recall": sum(r["recall"] for r in c_rows),
            "actionable": sum(r["actionable"] for r in c_rows),
            "eligible_unranked": sum(r["eligible"] and not r["recall"] for r in c_rows),
        },
        "route_order": [{**r, "route_probability_rank": i + 1} for i, r in enumerate(c_order)],
        "execution": {
            "buy_allowed": False, "production_unchanged": True,
            "frozen_route_gate_passed": route_gate["raw_gate_passed"],
            "validated_route_gate_passed": route_gate["gate_passed"],
            "route_contract_status": route_gate["contract_status"],
            "recorded_route_contract_version": route_gate["recorded_contract_version"],
            "route_gate_issue": route_gate["gate_issue"],
            "funding_evidence": [{k: w.get(k) for k in ("trade_date", "status", "record_count", "observed_at")}
                                 for w in funding],
            "funding_gate": "not_verified_by_research" if funding_current else "blocked_missing_or_stale",
            "status": "not_evaluated" if funding_current and route_gate.get("gate_passed") is True else "blocked",
            "remaining_gates": ["fund_source_provenance", "quote_freshness", "intraday_confirmation",
                                "limit_price", "liquidity", "lot100", "risk", "capacity", "fees_slippage", "T+1"],
        },
        "limitations": [
            "Post-hoc frozen snapshot analysis, not a pre-registered forward shadow experiment.",
            "No daily final limit-up, K-line outcome, future labels or fills used to select candidates.",
            "More route exposure is not improved precision, recall, profitability or fillability.",
            "Cost-after outcomes remain unknown; requires forward paired samples and real execution evidence.",
            "Explicit run selection does not reproduce production latest-run selection.",
        ],
    }


async def analyze_database(database: Path, *, run_id: int, decision_at: datetime,
                           contract: RouteQuotaContract, quote_as_of: datetime | None = None) -> dict:
    """Physically read-only SQLite URI plus query_only, one consistent read transaction."""
    database = database.expanduser().resolve()
    if not database.is_file():
        raise ValueError("database does not exist")
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{database.as_uri()}?mode=ro&uri=true",
        connect_args={"timeout": 30},
    )
    try:
        async with engine.connect() as connection:
            await connection.execute(text("PRAGMA query_only=ON"))
            await connection.execute(text("BEGIN"))
            async with AsyncSession(bind=connection, autoflush=False) as db:
                run = await db.get(PromotionPredictionRun, run_id)
                if run is None:
                    raise ValueError("run not found; no fallback")
                # Fetch the entire immutable batch BEFORE any lane/route filtering.
                snapshots = list((await db.scalars(select(PromotionPredictionSnapshot).where(
                    PromotionPredictionSnapshot.run_id == run_id
                ).order_by(PromotionPredictionSnapshot.id))).all())
                return compare_frozen_run(run, snapshots, decision_at=decision_at,
                                          quote_as_of=quote_as_of, contract=contract)
    finally:
        await engine.dispose()
