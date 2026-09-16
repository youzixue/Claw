"""Read-only training data from whole immutable prediction runs, never legacy rows."""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
from app.models.governance import TradeCalendarModel, DataQualityRun
from app.promotion.outcome_evidence import (next_recorded_trade_day, formal_outcome_bar_error,
                                           OUTCOME_EVIDENCE_VERSION, _FORMAL_CLOSE_SOURCES,
                                           require_paired_material_rows)
from app.promotion.ledger import _candidate_identity, _hash
from collections import defaultdict
from datetime import date, datetime, timedelta
from dataclasses import replace

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import settings
from app.core.trade_calendar import is_official_closed_day
from app.models.promotion import PromotionPredictionRun, PromotionPredictionSnapshot
from app.models.stock import StockKline, LimitUpPool
from app.promotion.labels import PROMOTION_LABEL_VERSION, promotion_event_label
from app.promotion.modeling.dataset import DatasetBundle
from app.promotion.modeling.feature_coverage import require_hist_feature_coverage
from app.promotion.modeling.features import FEATURE_VERSION, FeatureRow, extract_point_in_time_features
from app.promotion.regime import REGIME_LABELS
from app.promotion.direction_research import (DIRECTION_LABEL_VERSION, frozen_direction_probability,
                                            validate_frozen_direction_universe)
from app.promotion.outcome_evidence import _is_completed_outcome_date

LEDGER_DATASET_VERSION = "promotion_ledger_dataset_v4_sealed_outcomes"
_CLOSE_CONTEXTS = {"promotion_1510", "promotion_2000"}


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _loads(raw) -> dict:
    try:
        parsed = json.loads(raw or "{}")
    except (ValueError, TypeError) as exc:
        raise ValueError("invalid frozen factors JSON") from exc
    if not isinstance(parsed, dict):
        raise ValueError("frozen factors must be an object")
    return parsed


def _clock(value) -> bool:
    return isinstance(value, datetime) and value.tzinfo is None


def _day(value: date) -> bool:
    return value.weekday() < 5 and not is_official_closed_day(value)


def _positive_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0


def _batch_error(run, snapshots, cutoff):
    if run.status != "completed":
        return "latest_run_not_completed"
    if any(not _clock(v) for v in (run.as_of_at, run.created_at, run.completed_at)):
        return "run_clock_missing"
    if not run.as_of_at <= run.created_at <= run.completed_at <= cutoff:
        return "run_clock_invalid_or_future"
    if run.as_of_at.date() != run.reference_trade_date or not _day(run.reference_trade_date):
        return "run_day_mismatch"
    if run.gate_passed is not True:
        return "immutable_run_quality_not_passed"
    if len(snapshots) != run.candidate_count:
        return "incomplete_batch"
    if len({s.record_key for s in snapshots}) != len(snapshots):
        return "duplicate_snapshot_identity"
    if any(not _clock(s.created_at) or not run.as_of_at <= s.created_at <= run.completed_at for s in snapshots):
        return "snapshot_clock_missing_or_invalid"
    try:
        metadata = _loads(run.metadata_json)
        identities, components = [], {}
        for s in snapshots:
            factors = _loads(s.features_json)
            identity = _candidate_identity({
                "code": s.code, "target_board": s.target_board, "candidate_route": s.candidate_route,
                "raw_probability": s.raw_probability, "probability": s.calibrated_probability,
                "probability_factors": factors}, s.prediction_trade_date)
            if _hash(identity) != s.record_key:
                return "snapshot_feature_hash_or_identity_mismatch"
            for name in ("pool_rank", "rank_position", "recall_rank_position"):
                if (getattr(s, name) or 0) != identity[name]:
                    return "snapshot_rank_identity_mismatch"
            if s.rank_scope != identity["rank_scope"]:
                return "snapshot_rank_identity_mismatch"
            lane = components.setdefault(str(s.target_board), {})
            for name in ("model", "feature", "data"):
                lane.setdefault(name + "_versions", set()).add(
                    str(factors.get("prediction_" + name + "_version") or getattr(run, name + "_version")))
            identities.append(identity)
        identities.sort(key=lambda i: (i["target_board"], i["prediction_trade_date"], i["code"], i["candidate_route"]))
        if _hash(identities) != run.payload_hash:
            return "full_batch_payload_hash_mismatch"
        components = {k: {n: sorted(v) for n, v in lane.items()} for k, lane in components.items()}
        if metadata.get("model_components_by_target") != components:
            return "producer_version_manifest_mismatch"
        for name, prefix in (("model", "promotion_composite"), ("feature", "promotion_features_composite"),
                             ("data", "promotion_data_composite")):
            values = sorted({v for lane in components.values() for v in lane[name + "_versions"]})
            expected = values[0][:80] if len(values) == 1 else prefix + "_" + _hash(values)[:20]
            if getattr(run, name + "_version") != expected:
                return "producer_run_version_mismatch"
        batch_keys = metadata.get("batch_keys")
        if not isinstance(batch_keys, list) or not batch_keys or "|".join(batch_keys) != run.snapshot_batch_key:
            return "batch_key_manifest_missing"
        if _hash({"model_version": run.model_version, "feature_version": run.feature_version,
                  "data_version": run.data_version, "source": run.snapshot_source,
                  "context": run.snapshot_context, "batch_keys": batch_keys,
                  "payload_hash": run.payload_hash}) != run.run_key:
            return "run_identity_hash_mismatch"
    except (ValueError, TypeError, KeyError, AttributeError):
        return "invalid_frozen_identity"
    return None


def _quality_complete(audits, day, cutoff, counts, minimum_completeness):
    """Presence-only limit watermarks cannot prove a full negative-label universe."""
    eligible = [a for a in audits if a.trade_date == day and a.snapshot_context in _CLOSE_CONTEXTS
                and _clock(a.started_at) and _clock(a.completed_at)
                and datetime.combine(day, datetime.min.time()).replace(hour=15, minute=10)
                <= a.started_at <= a.completed_at <= cutoff]
    if not eligible:
        return False
    audit = max(eligible, key=lambda a: (a.completed_at, a.id))
    if not audit.gate_passed or audit.status not in {"ok", "passed"}:
        return False
    try:
        marks = _loads(audit.summary_json).get("watermarks", [])
        for dataset, count in counts.items():
            matches = [m for m in marks if m.get("dataset") == dataset and m.get("trade_date") == day.isoformat()]
            if len(matches) != 1:
                return False
            mark = matches[0]
            expected = mark.get("expected_count")
            if (mark.get("status") != "ok" or not _positive_number(expected)
                    or mark.get("record_count") != count
                    or mark.get("details", {}).get("coverage_scope") != "all_rows"
                    or count / expected < minimum_completeness
                    or not _positive_number(mark.get("completeness"))
                    or mark["completeness"] < minimum_completeness
                    or (dataset == "limit_up_pool" and (count != expected or mark["completeness"] != 1.0))):
                return False
        return True
    except (ValueError, TypeError, AttributeError):
        return False


async def build_ledger_training_dataset(
    db: AsyncSession, *, target_board: int, start_date: date | None = None,
    end_date: date | None = None, snapshot_context: str = "promotion_2000",
    as_of_at: datetime | None = None, model_version: str | None = None,
    identity_index=None, outcome_index=None, label_target: str = "promotion",
) -> DatasetBundle:
    """Select newest whole batch per date first; a bad newest run never falls back.

    Labels require shared sealed close/pool evidence in addition to the original
    whole-market/individual read-time checks. Any disagreement blocks the batch.
    No mutable fallback, archive publication, retrospective first-knowledge claim
    or production feature/Champion change is permitted here.
    """
    if label_target not in {"promotion", DIRECTION_LABEL_VERSION}:
        raise ValueError("unsupported ledger label target")
    if label_target == DIRECTION_LABEL_VERSION and target_board != 1:
        raise ValueError("direction research currently uses the frozen first-board candidate universe")
    label_version = PROMOTION_LABEL_VERSION if label_target == "promotion" else DIRECTION_LABEL_VERSION
    if db.new or db.dirty or db.deleted:
        raise ValueError("ledger research requires a clean session; refusing implicit autoflush")
    cutoff = as_of_at if as_of_at is not None else datetime.now()
    if not _clock(cutoff):
        raise ValueError("as_of_at must be a naive Asia/Shanghai clock")
    if target_board not in {1, 2} or snapshot_context not in _CLOSE_CONTEXTS:
        raise ValueError("M0 ledger training supports target 1/2 and close contexts only")
    statement = select(PromotionPredictionRun).where(
        PromotionPredictionRun.snapshot_source == "schedule",
        PromotionPredictionRun.snapshot_context == snapshot_context,
    )
    if start_date is not None:
        statement = statement.where(PromotionPredictionRun.reference_trade_date >= start_date)
    if end_date is not None:
        statement = statement.where(PromotionPredictionRun.reference_trade_date <= end_date)
    if model_version is not None:
        statement = statement.where(PromotionPredictionRun.model_version == model_version)
    runs = list((await db.scalars(statement)).all())
    by_date = {}
    for run in runs:
        previous = by_date.get(run.reference_trade_date)
        key = (run.as_of_at if _clock(run.as_of_at) else datetime.max, run.id)
        previous_key = ((previous.as_of_at if _clock(previous.as_of_at) else datetime.max), previous.id) if previous else None
        if previous is None or key > previous_key:
            by_date[run.reference_trade_date] = run
    selected = [by_date[day] for day in sorted(by_date)]
    snapshots_by_run = defaultdict(list)
    if selected:
        # All targets/routes/eligibilities first, no SQL LIMIT, no stockwise retry.
        snapshots = list((await db.scalars(select(PromotionPredictionSnapshot).where(
            PromotionPredictionSnapshot.run_id.in_([r.id for r in selected])
        ).order_by(PromotionPredictionSnapshot.id))).all())
        for snapshot in snapshots:
            snapshots_by_run[snapshot.run_id].append(snapshot)
    diagnostics = {
        "dataset_source": "prediction_snapshots", "storage_source": "immutable_prediction_ledger",
        "dataset_contract": LEDGER_DATASET_VERSION, "as_of_at": cutoff.isoformat(),
        "loaded_runs": len(runs), "selected_run_ids": [r.id for r in selected],
        "superseded_runs": len(runs) - len(selected), "excluded_runs": [],
        "snapshot_context": snapshot_context, "label_version": label_version,
        "label_source": "sealed_daily_truth_with_read_time_agreement",
        "label_first_known_at": None, "historical_label_availability_verified": False,
        "unknown_outcome_candidates": 0,
        "extractor_feature_version": FEATURE_VERSION,
        "producer_feature_contract": "ledger_identity_and_lane_manifest_not_extractor_version",
        "selected_batch_evidence_hash": _digest([
            {"id": r.id, "key": r.run_key, "payload": r.payload_hash, "metadata": r.metadata_json,
             "snapshots": [(s.id, s.record_key, s.features_json, s.target_board, s.code)
                           for s in snapshots_by_run[r.id]]} for r in selected]),
    }
    prepared = []
    for run in selected:
        full = snapshots_by_run[run.id]
        error = _batch_error(run, full, cutoff)
        lane = [s for s in full if s.target_board == target_board]
        if not error and (not lane or any(s.prediction_trade_date != run.reference_trade_date
                                         or s.horizon_days != 1 for s in lane)):
            error = "empty_or_inconsistent_target_lane"
        if not error and len({s.code for s in lane}) != len(lane):
            error = "duplicate_stock_in_target_lane"
        if error:
            diagnostics["excluded_runs"].append({"run_id": run.id, "reason": error})
            continue
        try:
            candidates = []
            for s in lane:
                probability = s.calibrated_probability
                if (not isinstance(probability, (int, float)) or isinstance(probability, bool)
                        or not math.isfinite(probability) or not 0 <= probability <= 1):
                    raise ValueError("invalid baseline probability")
                factors = _loads(s.features_json)
                if label_target == DIRECTION_LABEL_VERSION:
                    probability = frozen_direction_probability(factors)
                candidates.append((s, factors, FeatureRow(
                    code=s.code, trade_date=s.prediction_trade_date.isoformat(), target_board=target_board,
                    label=0, baseline_probability=probability, candidate_route=s.candidate_route,
                    values=extract_point_in_time_features(factors),
                    market_regime=factors.get("market_regime") if factors.get("market_regime") in REGIME_LABELS else "unknown",
                    hist_materialization=factors.get("hist_materialization"), feature_as_of_at=run.as_of_at,
                )))
            if label_target == DIRECTION_LABEL_VERSION:
                validate_frozen_direction_universe([(s.code, factors) for s, factors, _ in candidates])
            required = set().union(*(r.values.keys() for _, _, r in candidates))
            require_hist_feature_coverage([r for _, _, r in candidates], required, feature_version=FEATURE_VERSION)
        except ValueError as exc:
            diagnostics["excluded_runs"].append({"run_id": run.id, "reason": str(exc)})
            continue
        prepared.append((run, candidates))
    scopes = {(r.model_version, r.feature_version, r.data_version) for r, _ in prepared}
    if len(scopes) > 1:
        raise ValueError("mixed model/feature/data scope; specify a homogeneous date/model scope")
    from app.promotion.identity_evidence import prepare_identity_index, identity_pair_gate, PROFILE as IDENTITY_PROFILE
    if identity_index is None:
        identity_index = await prepare_identity_index(known_cutoff=cutoff)
    diagnostics["identity_profile"] = IDENTITY_PROFILE
    diagnostics["identity_index_hash"] = identity_index.evidence_hash
    diagnostics["identity_gates"] = []
    from app.promotion.outcome_materials import prepare_outcome_index, outcome_pair_gate, PROFILE as MATERIAL_PROFILE
    if outcome_index is None:
        try:
            outcome_index = await prepare_outcome_index(known_cutoff=cutoff)
        except Exception as exc:
            diagnostics["outcome_material_prepare_error"] = type(exc).__name__
    diagnostics["outcome_material_profile"] = MATERIAL_PROFILE
    diagnostics["outcome_material_gates"] = []
    rows, content = [], []
    if prepared:
        minimum = min(r.reference_trade_date for r, _ in prepared)
        bars = list((await db.scalars(select(StockKline).where(
            StockKline.trade_date >= minimum - timedelta(days=60),
            StockKline.trade_date <= cutoff.date(),
        ))).all())
        bars_by_date = defaultdict(dict)
        for bar in bars:
            if _day(bar.trade_date) and bar.source in _FORMAL_CLOSE_SOURCES:
                bars_by_date[bar.trade_date][bar.code] = bar
        market_dates = sorted(bars_by_date)
        calendar_rows = list((await db.scalars(select(TradeCalendarModel).where(
            TradeCalendarModel.trade_date >= minimum,
            TradeCalendarModel.trade_date <= cutoff.date()))).all())
        calendar = {r.trade_date: r.is_trade_day for r in calendar_rows}
        audits = list((await db.scalars(select(DataQualityRun).where(
            DataQualityRun.trade_date >= minimum,
            DataQualityRun.trade_date <= cutoff.date()))).all())
        diagnostics["label_quality_evidence"] = [
            {"id": a.id, "day": a.trade_date, "context": a.snapshot_context,
             "status": a.status, "gate": a.gate_passed, "started": a.started_at,
             "completed": a.completed_at, "summary": a.summary_json}
            for a in sorted(audits, key=lambda a: a.id)]
        diagnostics["calendar_evidence"] = sorted((d.isoformat(), state) for d, state in calendar.items())
        events = list((await db.scalars(select(LimitUpPool).where(
            LimitUpPool.trade_date >= minimum,
            LimitUpPool.trade_date <= cutoff.date(),
        ))).all())
        diagnostics["read_time_truth_hash"] = _digest({
            "bars": [(b.id, b.code, b.trade_date, b.source, str(b.close), str(b.prev_close), str(b.volume))
                     for b in sorted(bars, key=lambda b: b.id)],
            "events": [(e.id, e.code, e.trade_date, e.quarantined, e.source)
                       for e in sorted(events, key=lambda e: e.id)]})
        events_by_date = defaultdict(set)
        quarantined = defaultdict(set)
        for event in events:
            (quarantined if event.quarantined else events_by_date)[event.trade_date].add(event.code)
        minimum_rows = max(1, int(settings.PROMOTION_SHADOW_MIN_KLINE_ROWS))
        minimum_completeness = min(max(float(settings.PROMOTION_SHADOW_MIN_KLINE_COMPLETENESS), 0.0), 1.0)
        diagnostics["outcome_policy"] = {"minimum_kline_rows": minimum_rows,
                                         "minimum_kline_completeness": minimum_completeness,
                                         "evidence_version": OUTCOME_EVIDENCE_VERSION,
                                         "formal_sources": sorted(_FORMAL_CLOSE_SOURCES),
                                         "quality_contract": "recorded_all_rows_denominators_v1"}
        for run, candidates in prepared:
            outcome_day, calendar_error = next_recorded_trade_day(
                run.reference_trade_date, calendar, through=cutoff.date())
            if calendar_error:
                diagnostics["excluded_runs"].append({"run_id": run.id, "reason": calendar_error})
                continue
            if not _is_completed_outcome_date(outcome_day, now=cutoff):
                diagnostics["excluded_runs"].append({"run_id": run.id, "reason": "outcome_session_not_closed_or_missing"})
                continue
            recent_peak = max((len(bars_by_date[d]) for d in market_dates
                               if outcome_day - timedelta(days=60) <= d <= outcome_day), default=0)
            required_rows = max(minimum_rows, math.ceil(recent_peak * minimum_completeness))
            current_bars = bars_by_date.get(run.reference_trade_date, {})
            future_bars = bars_by_date.get(outcome_day, {})
            quality_ok = all(_quality_complete(audits, d, run.as_of_at if d == run.reference_trade_date else cutoff,
                {"stock_kline": len(bars_by_date.get(d, {})), "limit_up_pool": len(events_by_date.get(d, set()))},
                minimum_completeness) for d in (run.reference_trade_date, outcome_day))
            if (not quality_ok or len(current_bars) < required_rows or len(future_bars) < required_rows
                    or not events_by_date.get(run.reference_trade_date)
                    or not events_by_date.get(outcome_day)):
                diagnostics["excluded_runs"].append({"run_id": run.id, "reason": "authoritative_outcome_quality_incomplete"})
                continue
            identity_gate = identity_pair_gate(identity_index,
                codes=tuple(s.code for s, _, _ in candidates), prediction_at=run.as_of_at,
                outcome_day=outcome_day, evaluation_as_of=cutoff)
            diagnostics["identity_gates"].append({"run_id": run.id, **identity_gate})
            if not identity_gate["passed"]:
                diagnostics["excluded_runs"].append({"run_id": run.id,
                    "reason": "candidate_identity_unknown_or_outside_profile",
                    "identity_evidence_hash": identity_gate["evidence_hash"]})
                continue
            day_rows, day_content, unknown = [], [], []
            for s, factors, row in candidates:
                before = current_bars.get(s.code)
                after = future_bars.get(s.code)
                if (formal_outcome_bar_error(before, after)
                        or s.code in quarantined[run.reference_trade_date] or s.code in quarantined[outcome_day]):
                    unknown.append(s.code)
                    continue
                label = promotion_event_label(target_board=target_board,
                    prediction_day_limit_up=s.code in events_by_date[run.reference_trade_date],
                    outcome_limit_up=s.code in events_by_date[outcome_day])
                day_rows.append(replace(row, label=label))
                day_content.append({
                    "run_id": run.id, "run_key": run.run_key, "payload_hash": run.payload_hash,
                    "snapshot_id": s.id, "record_key": s.record_key, "features": factors,
                    "model_scope": (run.model_version, run.feature_version, run.data_version),
                    "clocks": (run.as_of_at, run.created_at, run.completed_at, s.created_at),
                    "baseline": row.baseline_probability, "route": row.candidate_route,
                    "code": s.code, "prediction_date": s.prediction_trade_date,
                    "outcome_date": outcome_day, "label": label,
                    "identity_evidence_hash": identity_gate["evidence_hash"],
                    "truth": {"prediction_limit": s.code in events_by_date[run.reference_trade_date],
                              "outcome_limit": s.code in events_by_date[outcome_day],
                              "prediction_bar": (before.id, before.close, before.prev_close, before.volume, before.source),
                              "outcome_bar": (after.id, after.close, after.prev_close, after.volume, after.source)},
                })
            if unknown:
                # Do not selectively drop bad/unknown names to improve paired
                # denominators: quarantine the whole day until truth is complete.
                diagnostics["unknown_outcome_candidates"] += len(unknown)
                diagnostics["excluded_runs"].append({"run_id": run.id, "reason": "candidate_outcome_unknown", "codes": sorted(unknown)})
                continue
            material_gate = {"passed": False, "profile": MATERIAL_PROFILE,
                "reasons": ["outcome_material_gate_unavailable"], "per_code": [],
                "evidence_hash": _digest({"unavailable": True, "run_id": run.id,
                    "prediction_day": run.reference_trade_date, "outcome_day": outcome_day})}
            try:
                if outcome_index is None:
                    raise ValueError("outcome_material_index_unavailable")
                returned = await asyncio.to_thread(outcome_pair_gate, outcome_index,
                    codes=tuple(s.code for s, _, _ in candidates), prediction_at=run.as_of_at,
                    prediction_day=run.reference_trade_date, outcome_day=outcome_day,
                    evaluation_as_of=cutoff)
                if type(returned) is not dict:
                    raise ValueError("outcome_material_gate_contract_invalid")
                material_gate = {k: returned.get(k) for k in ("passed", "profile", "reasons", "per_code", "evidence_hash")}
                sealed = require_paired_material_rows(material_gate, profile=MATERIAL_PROFILE,
                    read_time_rows=[{"code": s.code,
                        "prediction_limit_up": s.code in events_by_date.get(run.reference_trade_date, set()),
                        "outcome_limit_up": s.code in events_by_date.get(outcome_day, set()),
                        "before_close": current_bars[s.code].close,
                        "after_close": future_bars[s.code].close,
                        "after_prev_close": future_bars[s.code].prev_close} for s, _, _ in candidates])
            except Exception as exc:
                diagnostics["outcome_material_gates"].append({"run_id": run.id, **material_gate})
                diagnostics["excluded_runs"].append({"run_id": run.id,
                    "reason": "sealed_outcome_material_unverified",
                    "detail": str(exc) if isinstance(exc, ValueError) else type(exc).__name__,
                    "outcome_material_evidence_hash": material_gate.get("evidence_hash")})
                continue
            diagnostics["outcome_material_gates"].append({"run_id": run.id, **material_gate})
            day_rows = [replace(row, label=(
                int(sealed[row.code]["after_close"] > sealed[row.code]["after_prev_close"])
                if label_target == DIRECTION_LABEL_VERSION else
                promotion_event_label(target_board=target_board,
                    prediction_day_limit_up=sealed[row.code]["prediction_limit_up"],
                    outcome_limit_up=sealed[row.code]["outcome_limit_up"])
            )) for row in day_rows]
            sealed_labels = {row.code: row.label for row in day_rows}
            for item in day_content:
                item["label"] = sealed_labels[item["code"]]
                item["outcome_material_evidence_hash"] = material_gate["evidence_hash"]
            if label_target == DIRECTION_LABEL_VERSION:
                # Validate the WHOLE batch above, then use the same prediction-time
                # eligibility as the visible direction board, never outcome filters.
                eligible_codes = {s.code for s, factors, _ in candidates
                                  if factors["direction_research"]["eligible"] is True}
                day_rows = [row for row in day_rows if row.code in eligible_codes]
            rows.extend(day_rows)
            content.extend(day_content)
    required = set().union(*(row.values.keys() for row in rows)) if rows else set()
    require_hist_feature_coverage(rows, required, feature_version=FEATURE_VERSION)
    rows.sort(key=lambda r: (r.trade_date, r.code))
    content.sort(key=lambda r: (r["prediction_date"], r["code"]))
    data_version = f"promotion_ledger_{len(rows)}_{_digest({'version': LEDGER_DATASET_VERSION, 'label': label_version, 'rows': content, 'policy_and_exclusions': diagnostics})[:24]}"
    diagnostics.update({
        "eligible_records": len(rows), "positive_count": sum(r.label for r in rows),
        "negative_count": sum(1 - r.label for r in rows),
        "trade_day_count": len({r.trade_date for r in rows}),
        "eligible_run_ids": sorted({r["run_id"] for r in content}),
        "model_scope": sorted(scopes), "content_hash": _digest(content),
        "regime_coverage": sum(r.market_regime != "unknown" for r in rows) / len(rows) if rows else 0.0,
    })
    return DatasetBundle(rows=rows, diagnostics=diagnostics, data_version=data_version,
                         start_date=date.fromisoformat(rows[0].trade_date) if rows else None,
                         end_date=date.fromisoformat(rows[-1].trade_date) if rows else None)
