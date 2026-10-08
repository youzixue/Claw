"""Immutable observed-now fixed-price-session research, never fill authority.

The 15:00 ordinary quote and independently reported after-hours amounts remain
separate. Unclassified supplier daily volume is NEVER blindly added to post volume.
Local acceptance is not a physical DB commit receipt or historical PIT certificate.
"""
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.models.stock import StockAfterHoursObservation

PROTOCOL = "after_hours_research_v1"
THS_VERSION = "ths_v6_tail_9_10_cross_source_v1"
TENCENT_VERSION = "tencent_native_after_hours_v1_20261002"
SINA_VERSION = "sina_native_after_hours_v1_20261002"
SUPPLIER_DAILY_BASIS = "supplier_native_daily_total_after_session"
SUPPLIER_VERSIONS = {"tencent_after_hours": TENCENT_VERSION, "sina_after_hours": SINA_VERSION}
VALUES = ("close_price_1500", "regular_volume_shares", "regular_amount_yuan",
          "after_volume_shares", "after_amount_yuan", "reported_daily_volume",
          "reported_daily_amount", "all_day_volume_shares", "all_day_amount_yuan")
MAX_READ_ROWS = 20000


def _clock(value):
    if not isinstance(value, datetime) or value.tzinfo is not None:
        raise ValueError("after-hours clocks require naive Asia/Shanghai datetime")
    return value


def _number(value, *, integer=False, positive=False):
    if value is None or isinstance(value, bool) or value == "":
        return None
    try:
        number = Decimal(str(value))
        if not number.is_finite() or number < 0 or (positive and number == 0):
            return None
        if integer:
            return int(number) if number == number.to_integral_value() and number <= 2**63 - 1 else None
        result = float(number)
        return result if result < float("inf") else None
    except (ValueError, TypeError, InvalidOperation, OverflowError):
        return None


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def ths_tail(fields):
    """[9]=shares/[10]=CNY: four-board THS/Sina named-field sample cross-check.
    [8] is unknown. This is not a vendor dictionary, market coverage or finality proof.
    """
    return {
        "after_volume_shares": _number(fields[9], integer=True) if len(fields) > 9 else None,
        "after_amount_yuan": _number(fields[10]) if len(fields) > 10 else None,
        "mapping_evidence": "four_board_official_ths_sina_cross_check_20261002",
        "mapping_scope": "sample_verified_not_full_universe_or_etf_certified",
        "source_version": THS_VERSION,
    }


def regular_close_material(spot, *, day, received_at):
    """Only a source quote published after close but BEFORE fixed-price matching.
    Tencent amount is an estimate in the existing parser and is not upgraded here.
    """
    source_at = getattr(spot, "source_quote_at", None)
    stamp = getattr(spot, "received_at", None)
    price = _number(getattr(spot, "price", None), positive=True)
    hands = _number(getattr(spot, "volume", None), integer=True)
    valid = (isinstance(source_at, datetime) and source_at.tzinfo is None
             and source_at.date() == day and time(15) <= source_at.time() < time(15, 5)
             and isinstance(stamp, datetime) and stamp.tzinfo is None
             and source_at <= stamp <= received_at and price is not None)
    # Legacy spot parser turns absent/broken volume into 0. Without raw presence
    # proof that zero is UNKNOWN here, unlike explicit official post-session zero.
    shares = hands * 100 if hands is not None and 0 < hands <= (2**63-1)//100 else None
    return {
        "close_price_1500": price if valid else None,
        "regular_volume_shares": shares if valid else None,
        "regular_amount_yuan": None,
        "price_basis": "tencent_unadjusted_quote" if valid else "unknown",
        "volume_basis": "tencent_normalized_hands_100_share_precision" if valid else "unknown",
        "regular_amount_reason": "existing_tencent_amount_is_vwap_estimate_not_native_measured",
        "reason": None if valid else "no_visible_post_close_pre_fixed_price_source_quote",
        "quote_round_id": getattr(spot, "quote_round_id", None) if valid else None,
        "source_quote_at": source_at if valid else None,
    }


def observation(*, code, day, stage, source, source_version, received_at, values,
                source_quote_at=None, accepted_at=None):
    if not isinstance(code, str) or re.fullmatch(r"[0-9]{6}", code) is None:
        raise ValueError("invalid after-hours security identity")
    if type(day) is not date or stage not in {"regular_close", "after_hours"}:
        raise ValueError("invalid after-hours date/stage")
    received_at = _clock(received_at)
    accepted_at = _clock(accepted_at or datetime.now(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None))
    if day > received_at.date() or received_at > accepted_at:
        raise ValueError("future or unordered after-hours observation")
    if source_quote_at is not None:
        _clock(source_quote_at)
        if source_quote_at.date() != day or source_quote_at > received_at:
            raise ValueError("invalid source clock")
    if (not isinstance(source, str) or not 0 < len(source) <= 32
            or not isinstance(source_version, str) or not 0 < len(source_version) <= 64):
        raise ValueError("source identity required")
    material = {key: _number(values.get(key), integer="volume" in key,
                              positive=key == "close_price_1500") for key in VALUES}
    extra_fields = ("price_basis", "volume_basis", "reported_daily_volume_basis",
                    "reported_daily_amount_basis", "regular_amount_reason", "reason",
                    "quote_round_id", "mapping_evidence", "mapping_scope",
                    "daily_total_basis", "session_state", "source_url", "response_hash",
                    "source_units", "rule_version", "source_attempts",
                    "session_state_basis", "amount_precision_yuan")
    material.update({key: values.get(key) for key in extra_fields})
    required = ("close_price_1500", "regular_volume_shares") if stage == "regular_close" else (
        "after_volume_shares", "after_amount_yuan")
    missing = [key for key in required if material[key] is None]
    if stage == "regular_close" and (source_quote_at is None
            or not time(15) <= source_quote_at.time() < time(15, 5)
            or source_quote_at.date() != received_at.date()):
        missing.append("post_close_pre_fixed_price_source_clock")
    if stage == "after_hours" and received_at < datetime.combine(day, time(15, 30)):
        missing.append("matching_session_not_ended")
    post_volume, post_amount = material["after_volume_shares"], material["after_amount_yuan"]
    if stage == "after_hours" and post_volume is not None and post_amount is not None:
        if (post_volume == 0) != (post_amount == 0):
            missing.append("inconsistent_zero_volume_amount")
    if stage == "after_hours" and material.get("daily_total_basis"):
        basis = material["daily_total_basis"]
        recognized_basis = (basis == "official_daily_total_after_closed_session"
                            or (basis == SUPPLIER_DAILY_BASIS
                                and SUPPLIER_VERSIONS.get(source) == source_version))
        if (not recognized_basis or material.get("session_state") != "closed"
                or source_quote_at is None or source_quote_at.time() < time(15, 30)):
            missing.append("unverified_daily_total_basis")
        for total, part in (("all_day_volume_shares", "after_volume_shares"),
                            ("all_day_amount_yuan", "after_amount_yuan")):
            if material[total] is not None and material[part] is not None and material[total] < material[part]:
                missing.append("daily_total_less_than_after_hours")
    payload = {
        "protocol": PROTOCOL, "code": code, "trade_date": day.isoformat(), "stage": stage,
        "source": source, "source_version": source_version, "values": material,
        "missing": missing, "status": "partial" if missing else "observed",
        "source_finality_verified": False, "historical_pit": False,
        "trading_authority": False, "automatic_weight_update": False,
        "availability_basis": "local_acceptance_before_db_commit_not_commit_receipt",
    }
    return StockAfterHoursObservation(
        code=code, trade_date=day, stage=stage, source=source, source_version=source_version,
        content_hash=hashlib.sha256(_json(payload).encode()).hexdigest(), payload_json=_json(payload),
        source_quote_at=source_quote_at, received_at=received_at, recorded_at=accepted_at,
        available_at=accepted_at, quality_status=payload["status"], protocol_version=PROTOCOL)


async def append_observations(db, rows):
    """Idempotent content insert; caller owns transaction, clocks never overwritten."""
    if not rows:
        return {"inserted": 0}
    dialect = db.get_bind().dialect.name
    if dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
    elif dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        raise ValueError("unsupported after-hours storage dialect")
    records = [{column.name: getattr(row, column.name)
                for column in StockAfterHoursObservation.__table__.columns if column.name != "id"}
               for row in rows]
    inserted = 0
    for start in range(0, len(records), 50):
        result = await db.execute(insert(StockAfterHoursObservation).values(records[start:start+50])
            .on_conflict_do_nothing(index_elements=[
                "code", "trade_date", "stage", "source", "source_version", "content_hash"]))
        inserted += result.rowcount
    return {"inserted": inserted}


def research_features(regular, post):
    """No estimated amount, unclassified daily volume, or unknown zero denominator."""
    r = regular["values"] if regular and not regular["missing"] else {}
    p = post["values"] if post and not post["missing"] else {}
    regular_volume, after_volume = r.get("regular_volume_shares"), p.get("after_volume_shares")
    regular_amount, after_amount = r.get("regular_amount_yuan"), p.get("after_amount_yuan")
    total_volume = (regular_volume + after_volume
                    if regular_volume is not None and after_volume is not None else None)
    total_amount = (regular_amount + after_amount
                    if regular_amount is not None and after_amount is not None else None)
    basis = "separately_measured_regular_plus_post_not_supplier_daily_plus_post"
    # Closed official feeds expose daily totals separately. They already INCLUDE
    # post trades; never add post a second time. Subtraction is labelled derived,
    # not falsely described as a measured 15:00 baseline.
    if p.get("daily_total_basis") in {"official_daily_total_after_closed_session", SUPPLIER_DAILY_BASIS}:
        total_volume, total_amount = p.get("all_day_volume_shares"), p.get("all_day_amount_yuan")
        basis = ("observed_official_daily_total_after_closed_session"
                 if p["daily_total_basis"] == "official_daily_total_after_closed_session"
                 else "observed_supplier_native_daily_total_after_session")
    derived_volume = total_volume - after_volume if total_volume is not None and after_volume is not None else None
    derived_amount = total_amount - after_amount if total_amount is not None and after_amount is not None else None
    reasons = []
    close = r.get("close_price_1500") or p.get("close_price_1500")
    if close is None:
        reasons.append("closing_price_unavailable")
    if not p:
        reasons.append("independent_after_hours_missing_or_invalid")
    if total_volume is None:
        reasons.append("same_basis_daily_volume_unavailable")
    if total_amount is None:
        reasons.append("same_basis_daily_amount_unavailable")
    return {
        "close_price_1500": close,
        "closing_price_availability": "regular_close_observation" if r else "observed_after_session_not_visible_at_1500",
        "derived_regular_volume_shares": derived_volume, "derived_regular_amount_yuan": derived_amount,
        "regular_components_basis": "daily_minus_independent_post_not_pre_session_measurement",
        "regular_volume_shares": regular_volume, "regular_amount_yuan": regular_amount,
        "after_volume_shares": after_volume, "after_amount_yuan": after_amount,
        "all_day_volume_shares": total_volume, "all_day_amount_yuan": total_amount,
        "after_volume_ratio": after_volume / total_volume if total_volume and after_volume is not None else None,
        "after_amount_ratio": after_amount / total_amount if total_amount and after_amount is not None else None,
        "ratio_basis": basis,
        "volume_precision": r.get("volume_basis") or p.get("volume_basis"),
        "after_amount_precision_yuan": p.get("amount_precision_yuan"),
        "strength_interpretation": "activity_not_directional_order_flow",
        "source_finality_verified": False, "historical_pit": False, "trading_authority": False,
        "missing": reasons, "status": "partial" if reasons else "observed",
    }


async def read_after_hours(db, *, day, as_of, limit=100):
    """Bounded SELECT-only latest visible content. Missing table is caller's unavailable."""
    _clock(as_of)
    if type(day) is not date or day > as_of.date() or type(limit) is not int or not 1 <= limit <= 500:
        raise ValueError("invalid after-hours read window")
    rows = list((await db.scalars(select(StockAfterHoursObservation).where(
        StockAfterHoursObservation.trade_date == day,
        StockAfterHoursObservation.received_at <= as_of,
        StockAfterHoursObservation.recorded_at <= as_of,
        StockAfterHoursObservation.available_at <= as_of,
    ).order_by(StockAfterHoursObservation.available_at.desc(),
               StockAfterHoursObservation.id.desc()).limit(MAX_READ_ROWS + 1))).all())
    latest = {}
    for row in rows[:MAX_READ_ROWS]:
        key = (row.code, row.stage)
        if key not in latest:
            try:
                payload = json.loads(row.payload_json)
                if (row.protocol_version != PROTOCOL or not isinstance(payload, dict)
                        or payload.get("protocol") != PROTOCOL or payload.get("code") != row.code
                        or payload.get("trade_date") != day.isoformat() or payload.get("stage") != row.stage
                        or payload.get("source") != row.source or payload.get("source_version") != row.source_version
                        or payload.get("status") != row.quality_status
                        or not isinstance(payload.get("values"), dict) or not isinstance(payload.get("missing"), list)
                        or not row.received_at <= row.recorded_at == row.available_at
                        or (row.source_quote_at is not None and (row.source_quote_at.date() != day
                                                               or row.source_quote_at > row.received_at))
                        or hashlib.sha256(_json(payload).encode()).hexdigest() != row.content_hash):
                    raise ValueError("latest observation integrity unavailable")
                latest[key] = (row, payload)
            except (ValueError, TypeError, AttributeError):
                # Do not conceal a corrupt latest revision by falling back to an older good row.
                latest[key] = (row, {"values": {}, "missing": ["latest_observation_integrity_unavailable"]})
    codes = sorted({code for code, stage in latest})
    items = []
    for code in codes[:limit]:
        regular, post = latest.get((code, "regular_close")), latest.get((code, "after_hours"))
        features = research_features(regular[1] if regular else None, post[1] if post else None)
        items.append({"code": code, **features,
            "observation_missing": {stage: pair[1].get("missing", []) for stage, pair in (
                ("regular_close", regular), ("after_hours", post)) if pair},
            "observation_ids": [pair[0].id for pair in (regular, post) if pair],
            "source_refs": [{"id": pair[0].id, "stage": pair[0].stage, "source": pair[0].source,
                "quality_status": pair[0].quality_status,
                "integrity_verified": pair[1].get("protocol") == PROTOCOL,
                "source_version": pair[0].source_version, "content_hash": pair[0].content_hash,
                "source_quote_at": pair[0].source_quote_at.isoformat() if pair[0].source_quote_at else None,
                "received_at": pair[0].received_at.isoformat(), "recorded_at": pair[0].recorded_at.isoformat(),
                "available_at": pair[0].available_at.isoformat()}
                for pair in (regular, post) if pair]})
    return {"status": "partial" if codes else "unavailable", "source": "stock_after_hours_observation",
            "protocol": PROTOCOL, "trade_date": day.isoformat(), "as_of_at": as_of.isoformat(),
            "items": items, "stored_code_count": len(codes), "returned_codes": len(items),
            "truncated": len(codes) > limit or len(rows) > MAX_READ_ROWS,
            "complete_market_coverage": False, "source_finality_verified": False,
            "historical_pit": False, "trading_authority": False}


SCHEDULER_RECEIPT_PROTOCOL = "after_hours_scheduler_event_v1_20261002"
AFTER_HOURS_JOB_SLOTS = {
    "after_hours_regular_baseline": time(15, 1),
    "after_hours_research_1535": time(15, 35),
    "after_hours_research_2010": time(20, 10),
}


def scheduler_event_receipt(event, *, observed_at):
    """Freeze bounded research-event leaves, never arbitrary retval/exception text.

    Row clocks describe event observation, not handler start or physical commit.
    This is not immutable history, first source availability or natural acceptance.
    """
    from apscheduler.events import (EVENT_JOB_EXECUTED, EVENT_JOB_ERROR,
                                   EVENT_JOB_MISSED, EVENT_JOB_MAX_INSTANCES)
    job_id = getattr(event, "job_id", None)
    if not isinstance(job_id, str) or job_id not in AFTER_HOURS_JOB_SLOTS:
        return None
    observed_at = _clock(observed_at)
    codes = {EVENT_JOB_EXECUTED: "executed", EVENT_JOB_ERROR: "error",
             EVENT_JOB_MISSED: "missed", EVENT_JOB_MAX_INSTANCES: "max_instances"}
    if type(event.code) is not int or event.code not in codes:
        raise ValueError("unsupported after-hours scheduler event")
    def local(value):
        if not isinstance(value, datetime):
            raise ValueError("scheduler event clock missing")
        return _clock(value.astimezone(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)
                      if value.tzinfo is not None else value)
    times = getattr(event, "scheduled_run_times", None)
    if times is not None:
        if not isinstance(times, list) or not 1 <= len(times) <= 1000:
            raise ValueError("scheduler event times budget")
        scheduled, first = local(times[-1]), local(times[0])
        count = len(times)
    else:
        scheduled = first = local(getattr(event, "scheduled_run_time", None))
        count = 1
    if not first <= scheduled <= observed_at:
        raise ValueError("scheduler event clock rollback or future")
    result = getattr(event, "retval", None)
    result = result if isinstance(result, dict) else {}
    outcomes = {"observed", "partial", "unavailable", "disabled", "busy", "blocked", "failed"}
    outcome = result.get("status")
    outcome = outcome if isinstance(outcome, str) and outcome in outcomes else "unknown"
    def integer(value):
        return value if type(value) is int and 0 <= value <= 10000 else None
    def handler_clock(value):
        if (not isinstance(value, str) or len(value) > 32 or re.fullmatch(
                r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:[.][0-9]{1,6})?", value) is None):
            return None
        try:
            return _clock(datetime.fromisoformat(value))
        except ValueError:
            return None
    started, completed = (handler_clock(result.get(k)) for k in ("started_at", "completed_at"))
    clock_ok = started is not None and completed is not None and scheduled <= started <= completed <= observed_at
    selected, inserted = (integer(result.get(k)) for k in ("selected_codes", "inserted"))
    raw_counts = result.get("status_counts")
    shape_ok = (isinstance(raw_counts, dict) and len(raw_counts) <= 2
                and all(key in {"observed", "partial"} for key in raw_counts))
    counts = {key: integer(value) for key, value in raw_counts.items()} if shape_ok else {}
    counts_ok = (shape_ok and selected is not None and inserted is not None and inserted <= selected
                 and all(value is not None for value in counts.values())
                 and sum(counts.values()) == selected)
    slot_ok = scheduled.time() == AFTER_HOURS_JOB_SLOTS[job_id]
    details = {
        "protocol": SCHEDULER_RECEIPT_PROTOCOL, "job_id": job_id,
        "scheduled_at": scheduled.isoformat(), "first_scheduled_at": first.isoformat(),
        "scheduled_time_count": count, "event_status": codes[event.code],
        "scheduled_slot_matches_registration": slot_ok, "handler_result_status": outcome,
        "handler_started_at": started.isoformat() if started else None,
        "handler_completed_at": completed.isoformat() if completed else None,
        "handler_clock_valid": clock_ok, "selected_codes": selected, "inserted": inserted,
        "status_counts": counts, "summary_valid": counts_ok,
        "handler_commit_ack_reported": result.get("committed") is True,
        "capture_summary_recorded": (event.code == EVENT_JOB_EXECUTED and slot_ok and clock_ok
            and counts_ok and result.get("committed") is True and outcome in {"observed", "partial", "unavailable"}),
        "error_type": type(event.exception).__name__[:80] if getattr(event, "exception", None) else None,
        "row_clock_scope": "scheduler_event_observed_not_handler_start_or_physical_commit",
        "observation_content_recheck_is_not_run_receipt": True,
        "natural_acceptance_verified": False, "source_first_availability_certified": False,
        "historical_pit": False, "execution_authorized": False,
    }
    # Duplicate terminal-event delivery preserves its first observation. A 20:10
    # invocation is distinct even if source-content insertion returned zero.
    logical_key = hashlib.sha256(_json({"protocol": SCHEDULER_RECEIPT_PROTOCOL,
        "job_id": job_id, "scheduled_at": scheduled.isoformat()}).encode()).hexdigest()
    text = _json(details)
    if len(text.encode()) > 8192:
        raise ValueError("scheduler event metadata byte budget")
    return {"run_key": logical_key, "logical_key": logical_key,
        "job_name": job_id, "review_date": scheduled.date(), "phase": "postmarket",
        "trigger": "scheduler_event", "attempt": 1, "status": codes[event.code],
        "quality_status": outcome, "details_json": text,
        "started_at": observed_at, "completed_at": observed_at}


async def append_scheduler_event_receipt(db, record):
    """Append to existing run storage; caller owns commit. No DDL or replay."""
    from app.models.review import ReviewAutomationRun
    if record.get("job_name") not in AFTER_HOURS_JOB_SLOTS:
        raise ValueError("only after-hours research scheduler receipts")
    dialect = db.get_bind().dialect.name
    if dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
    elif dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        raise ValueError("unsupported scheduler receipt storage dialect")
    result = await db.execute(insert(ReviewAutomationRun).values(**record)
        .on_conflict_do_nothing(index_elements=["run_key"]))
    status = "recorded"
    if result.rowcount != 1:
        # Compare in SQL, returning only a boolean, never hydrating arbitrary
        # existing details TEXT. Changed redelivery cannot overwrite first event.
        same = await db.scalar(select(ReviewAutomationRun.details_json == record["details_json"])
            .where(ReviewAutomationRun.run_key == record["run_key"]))
        status = "duplicate" if same is True else "conflict"
    return {"status": status, "run_key": record["run_key"], "execution_authorized": False}

