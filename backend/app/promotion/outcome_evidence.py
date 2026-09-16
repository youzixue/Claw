"""Shared strict next-session and formal-bar checks for model outcome research."""
from datetime import date, datetime, time, timedelta
import math

from app.core.trade_calendar import is_official_closed_day
from app.data.price_chain import (
    FORMAL_CLOSE_SOURCES as _FORMAL_CLOSE_SOURCES,
    PRICE_CHAIN_MAX_ABS_GAP as _PRICE_CHAIN_MAX_ABS_GAP,
)

OUTCOME_EVIDENCE_VERSION = "recorded_calendar_formal_close_v1"


def _is_completed_outcome_date(
    trade_date_value: date | None,
    *,
    now: datetime | None = None,
) -> bool:
    """Reject intraday labels: today\'s outcome is immutable only after 15:10."""
    if trade_date_value is None:
        return False
    if trade_date_value.weekday() >= 5 or is_official_closed_day(trade_date_value):
        return False
    current = now or datetime.now()
    if trade_date_value < current.date():
        return True
    if trade_date_value > current.date():
        return False
    return (current.hour, current.minute) >= (15, 10)


def next_recorded_trade_day(prediction_day, calendar, *, through):
    """Return (date, reason); never skip an unknown day or infer from available bars."""
    if (not isinstance(prediction_day, date) or isinstance(prediction_day, datetime)
            or not isinstance(through, date) or isinstance(through, datetime)):
        raise ValueError("outcome calendar requires dates")
    if calendar.get(prediction_day) is not True:
        return None, "prediction_calendar_unknown"
    if prediction_day.weekday() >= 5 or is_official_closed_day(prediction_day):
        return None, "calendar_conflict"
    cursor = prediction_day + timedelta(days=1)
    while cursor <= through:
        state = calendar.get(cursor)
        if state is None or (state is not True and state is not False):
            return None, "outcome_calendar_gap"
        if state is True:
            if cursor.weekday() >= 5 or is_official_closed_day(cursor):
                return None, "calendar_conflict"
            return cursor, ""
        cursor += timedelta(days=1)
    return None, "outcome_session_not_available"


def _positive(value):
    try:
        return (isinstance(value, (int, float)) and not isinstance(value, bool)
                and math.isfinite(value) and value > 0)
    except OverflowError:
        return False


def formal_outcome_bar_error(before, after):
    """Mutable close rows are read-time outcome material, never old arrival proof."""
    if before is None or after is None:
        return "candidate_outcome_bar_missing"
    if any(getattr(bar, "source", None) not in _FORMAL_CLOSE_SOURCES for bar in (before, after)):
        return "candidate_outcome_source_unverified"
    if not all(_positive(getattr(bar, name, None)) for bar in (before, after) for name in ("close", "volume")):
        return "candidate_outcome_suspended_or_invalid"
    previous = getattr(after, "prev_close", None)
    if not _positive(previous) or abs(previous - before.close) > _PRICE_CHAIN_MAX_ABS_GAP:
        return "candidate_outcome_price_chain_discontinuity"
    return ""


def require_paired_material_rows(gate, *, profile, read_time_rows):
    """Require a complete sealed label set AND agreement with current read facts.

    Only small owned projections are accepted. A current DB value is never a
    fallback for missing sealed evidence, nor can one bad name leave the cohort.
    """
    if (type(gate) is not dict or gate.get("profile") != profile
            or gate.get("passed") is not True or gate.get("reasons") != []):
        raise ValueError("formal_outcome_material_unverified")
    sha = gate.get("evidence_hash")
    if type(sha) is not str or len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
        raise ValueError("formal_outcome_material_hash_invalid")
    sealed = gate.get("per_code")
    if type(read_time_rows) is not list or not read_time_rows or type(sealed) is not list or len(sealed) != len(read_time_rows):
        raise ValueError("formal_outcome_material_denominator_invalid")
    fields = ("prediction_limit_up", "outcome_limit_up", "before_close", "after_close", "after_prev_close")
    def valid(row):
        return (type(row) is dict and type(row.get("code")) is str
                and len(row["code"]) == 6 and all("0" <= c <= "9" for c in row["code"])
                and all(type(row.get(k)) is bool for k in fields[:2])
                and all(_positive(row.get(k)) for k in fields[2:]))
    if any(not valid(row) for row in read_time_rows):
        raise ValueError("read_time_outcome_projection_invalid")
    if any(not valid(row) or row.get("status") != "verified" or row.get("reasons") != [] for row in sealed):
        raise ValueError("formal_outcome_material_row_invalid")
    current = {row["code"]: row for row in read_time_rows}
    evidence = {row["code"]: row for row in sealed}
    if len(current) != len(read_time_rows) or len(evidence) != len(sealed) or set(current) != set(evidence):
        raise ValueError("formal_outcome_material_denominator_invalid")
    if any(current[code][field] != evidence[code][field] for code in current for field in fields):
        raise ValueError("formal_outcome_material_read_time_conflict")
    return {code: {field: evidence[code][field] for field in fields} for code in sorted(evidence)}


FORWARD_SHADOW_VERSION = "scored_before_outcome_auction_v1"


def forward_shadow_clock_evidence(*, prediction_as_of, prediction_created,
                                  prediction_completed, artifact_created,
                                  shadow_created, shadow_completed, outcome_date,
                                  evaluation_at):
    """Scoring/replay may exist later, but only pre-auction scores are forward evidence."""
    clocks = {"prediction_as_of": prediction_as_of, "prediction_created": prediction_created,
              "prediction_completed": prediction_completed, "artifact_created": artifact_created,
              "shadow_created": shadow_created, "shadow_completed": shadow_completed,
              "evaluation_at": evaluation_at}
    valid = all(isinstance(value, datetime) and value.tzinfo is None for value in clocks.values())
    deadline = datetime.combine(outcome_date, time(9, 15))
    reason = ""
    if not valid:
        reason = "missing_or_invalid_forward_clock"
    elif not prediction_as_of <= prediction_created <= prediction_completed <= shadow_created <= shadow_completed <= evaluation_at:
        reason = "forward_clock_order_invalid"
    elif artifact_created > shadow_created:
        reason = "artifact_registered_after_scoring"
    elif shadow_completed >= deadline:
        reason = "scored_after_outcome_window_opened"
    return {"contract": FORWARD_SHADOW_VERSION, "passed": not reason, "reason": reason,
            "deadline_exclusive": deadline.isoformat(),
            **{key: value.isoformat() if isinstance(value, datetime) and value.tzinfo is None else None
               for key, value in clocks.items() if key != "evaluation_at"}}
