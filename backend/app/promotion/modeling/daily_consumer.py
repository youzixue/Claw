"""Optional research evidence on NEW 20:00 ledger records, after production ranking.

No collection, materialization, DB, model inference or writes. Default reviewed
policy is empty: missing evidence is visible, not converted to zero or permission.
Only immutable handles and owned code tuples cross worker threads, never a Session.
"""
from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
import math
import re
from threading import Lock

from app.promotion.modeling.daily_materialization import LockedReady, lock_ready, bind_candidates
from app.promotion.modeling.daily_materials import MaterialArchive, ReviewedSourcePolicy, clock
from app.promotion.modeling.feature_coverage import hist_feature_coverage
from app.promotion.modeling.features import COMMON_NUMERIC_FEATURES, FEATURE_VERSION

CONSUMER_VERSION = "promotion_daily_hist_consumer_v1"
STATUS_KEY = "daily_hist_research"
HIST_NAMES = frozenset(k for k in COMMON_NUMERIC_FEATURES if k.startswith("hist_"))
_READ_LOCK = Lock()  # Cancelled async readers must not launch overlapping heavy scans.


@dataclass(frozen=True)
class DailyHistContext:
    cutoff: datetime
    status: str
    reason: str
    locked: LockedReady | None = None


def _read_context(root, policy, cutoff):
    if not _READ_LOCK.acquire(blocking=False):
        return DailyHistContext(cutoff, "blocked", "material_reader_busy")
    try:
        archive = MaterialArchive(root, policy=policy)
        locked = lock_ready(archive, trade_date=cutoff.date(), cutoff=cutoff)
        return DailyHistContext(cutoff, "ready" if locked else "blocked",
                                "" if locked else "no_ready_before_cutoff", locked)
    except Exception:
        # A research archive error never invalidates or substitutes Champion data.
        return DailyHistContext(cutoff, "blocked", "ready_verification_failed")
    finally:
        _READ_LOCK.release()


async def prepare_daily_hist_context(*, snapshot_source, snapshot_context,
                                     request_started_at, archive_root,
                                     policy=None, timeout_sec=5.0):
    """Lock once at request entry; caller retains existing official clock/calendar gate."""
    if snapshot_source != "schedule" or snapshot_context != "promotion_2000":
        return None
    if clock(request_started_at) is None:
        raise ValueError("naive request_started_at required")
    if isinstance(timeout_sec, bool) or not isinstance(timeout_sec, (float, int)) or not math.isfinite(timeout_sec) or timeout_sec <= 0:
        raise ValueError("positive finite material read timeout required")
    cutoff = request_started_at.replace(microsecond=0)  # matches ledger Run.as_of_at
    policy = policy if policy is not None else ReviewedSourcePolicy()
    if not policy.validators:
        # No disk scan or guessed vendor approval, even if old ready files exist.
        return DailyHistContext(cutoff, "blocked", "source_protocol_unreviewed")
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_read_context, archive_root, policy, cutoff), timeout_sec)
    except TimeoutError:
        return DailyHistContext(cutoff, "blocked", "material_read_timeout")


def _candidate_error(item, context, trade_dates):
    code = item.get("code")
    target = item.get("target_board")
    factors = item.get("probability_factors")
    if not isinstance(code, str) or re.fullmatch(r"[0-9]{6}", code) is None or type(target) is not int or target not in (1, 2):
        return "candidate_identity_invalid"
    if not isinstance(factors, dict):
        return "candidate_factors_missing"
    if (factors.get("prediction_snapshot_source") != "schedule"
            or factors.get("prediction_snapshot_context") != "promotion_2000"
            or clock(factors.get("prediction_snapshot_recorded_at")) != context.cutoff
            or factors.get("prediction_candidate_anchor_trade_date") != context.cutoff.date().isoformat()
            or trade_dates.get(target) != context.cutoff.date()):
        return "candidate_clock_or_context_mismatch"
    if any(k in factors for k in (*HIST_NAMES, "hist_materialization", STATUS_KEY)):
        return "existing_hist_evidence_not_overwritten"
    return ""


async def attach_daily_hist_evidence(candidates, context, *, prediction_trade_dates):
    """Copy only new records/factors; retain every candidate, ranking and trade flag.

    The whole frozen batch uses one handle. Values/proofs are bound in a worker
    without rereading files or recomputing hist. No fallback to mutable tables.
    """
    if context is None:
        return candidates, {"status": "not_applicable", "consumer_version": CONSUMER_VERSION}
    if not isinstance(context, DailyHistContext):
        raise ValueError("DailyHistContext required")
    errors = [_candidate_error(item, context, prediction_trade_dates) for item in candidates]
    identities = Counter((item.get("code"), item.get("target_board")) for item in candidates
                         if isinstance(item.get("code"), str) and type(item.get("target_board")) is int)
    for index, item in enumerate(candidates):
        if not errors[index] and identities[(item["code"], item["target_board"])] != 1:
            errors[index] = "duplicate_candidate_lane_identity"
    codes = tuple(sorted({item["code"] for item, error in zip(candidates, errors) if not error}))
    bindings = {}
    batch_reason = context.reason
    if context.status == "ready" and context.locked is not None and codes:
        try:
            bindings = await asyncio.to_thread(bind_candidates, context.locked, codes=codes)
        except Exception:
            batch_reason = "candidate_binding_failed"
    output, counts = [], Counter()
    for item, error in zip(candidates, errors):
        result = bindings.get(item.get("code"), {}) if not error else {}
        reason = error or batch_reason or result.get("reason") or "hist_evidence_missing"
        proof = result.get("hist_materialization")
        values = result.get("values")
        ready = False
        if not error and result.get("status") == "ready" and isinstance(values, dict):
            # This integration accepts the full same-formula profile only. Never
            # inject output/rank fields from an archive or silently drop a group.
            try:
                coverage = hist_feature_coverage(values, HIST_NAMES, materialization=proof,
                    code=item["code"], trade_date=context.cutoff.date().isoformat(),
                    as_of_at=context.cutoff, feature_version=FEATURE_VERSION)
                ready = set(values) == HIST_NAMES and coverage["passed"]
            except (ValueError, TypeError, OverflowError):
                ready = False
            reason = "" if ready else "bound_hist_contract_invalid"
        metadata = {"consumer_version": CONSUMER_VERSION, "status": "ready" if ready else "blocked",
                    "reason": reason, "cutoff": context.cutoff.isoformat(),
                    "scope": "research_only_post_ranking_not_production_inference"}
        counts[metadata["status"]] += 1
        factors = dict(item.get("probability_factors") or {})
        # On collision preserve old evidence verbatim and report the reason in
        # the batch summary; never overwrite an earlier consumer's declaration.
        if STATUS_KEY not in factors:
            factors[STATUS_KEY] = metadata
        if ready:
            factors.update(values)
            factors["hist_materialization"] = proof
        output.append({**item, "probability_factors": factors})
        if not ready:
            counts["reason:" + reason] += 1
    summary = {"consumer_version": CONSUMER_VERSION, "status": "ready" if candidates and counts["blocked"] == 0 else "blocked",
               "cutoff": context.cutoff.isoformat(), "candidate_count": len(candidates),
               "ready_count": counts["ready"], "blocked_count": counts["blocked"],
               "blocked_reasons": {k[7:]: v for k, v in sorted(counts.items()) if k.startswith("reason:")},
               "production_inference_changed": False}
    return output, summary
