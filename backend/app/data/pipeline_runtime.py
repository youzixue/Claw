"""Bounded, read-only runtime diagnostics; never an execution/continuity waiver.

Liveness is not business success. This state covers only this process, is not a
historical ledger, and cannot prove that a job ran while the process was absent.
No payload rows, job arguments, exception text or credentials are retained.
"""
from collections import deque
from datetime import date, datetime

from apscheduler.events import (
    EVENT_JOB_ERROR, EVENT_JOB_EXECUTED, EVENT_JOB_MAX_INSTANCES, EVENT_JOB_MISSED,
)

from app.core.trade_calendar import TRADE_SESSIONS, is_official_closed_day
from app.data.fund_flow_clock import local_clock
from app.data.quote_round import quote_round_continuity


JOB_EVENT_MASK = (
    EVENT_JOB_ERROR | EVENT_JOB_EXECUTED | EVENT_JOB_MAX_INSTANCES | EVENT_JOB_MISSED
)


class PipelineRuntimeHealth:
    def __init__(self, *, started_at: datetime | None = None):
        self.started_at = local_clock(started_at) or datetime.now()
        self._calendar: tuple[date, bool] | None = None
        self._quote: dict = {}
        self._quote_at: datetime | None = None
        self._gaps: deque[dict] = deque(maxlen=100)
        self._jobs: dict[str, dict] = {}
        self._job_alerts: deque[dict] = deque(maxlen=100)

    def observe_calendar(self, trade_day: date, is_trade_day: bool) -> None:
        self._calendar = (trade_day, is_trade_day)

    def observe_quote(self, record: dict, *, previous_committed_at,
                      visible_at: datetime) -> dict:
        """Call only AFTER successful DB commit; failed commits are not heartbeats."""
        committed_at = local_clock(record.get("committed_at"))
        visible_at = local_clock(visible_at)
        continuity = quote_round_continuity(previous_committed_at, committed_at)
        publication_delay = (
            (visible_at - committed_at).total_seconds()
            if visible_at is not None and committed_at is not None else None
        )
        if (committed_at is None or visible_at is None or publication_delay < 0
                or (self._quote_at is not None and committed_at <= self._quote_at)):
            continuity = {**continuity, "status": "invalid_clock"}
        elif committed_at is not None:
            self._quote_at = committed_at
            self._quote = {
                "round_id": str(record.get("round_id") or ""),
                "quality_status": str(record.get("quality_status") or "unknown"),
                "quality_reason": str(record.get("quality_reason") or "")[:512],
                "committed_at": committed_at.isoformat(),
                "visible_at": visible_at.isoformat(),
                # Existing QuoteRound.committed_at precedes the DB commit. Keep
                # both clocks explicit, without rewriting it or calling delay zero.
                "commit_to_visibility_sec": round(publication_delay, 3),
                "code_version": str(record.get("code_version") or ""),
                "config_version": str(record.get("config_version") or ""),
                "intercommit_continuity": continuity,
            }
        if continuity["status"] in {"gap", "invalid_clock"}:
            self._gaps.append({
                "observed_at": (visible_at or datetime.now()).isoformat(),
                "round_id": str(record.get("round_id") or ""),
                **continuity,
            })
        return continuity

    def observe_job(self, event, *, observed_at: datetime | None = None) -> dict:
        now = local_clock(observed_at) or datetime.now()
        code = event.code
        status = {
            EVENT_JOB_ERROR: "error", EVENT_JOB_EXECUTED: "executed",
            EVENT_JOB_MISSED: "missed", EVENT_JOB_MAX_INSTANCES: "max_instances",
        }.get(code, "unknown")
        scheduled = local_clock(getattr(event, "scheduled_run_time", None))
        scheduled_times = getattr(event, "scheduled_run_times", None) or []
        if scheduled is None and scheduled_times:
            scheduled = local_clock(scheduled_times[-1])
        result = getattr(event, "retval", None)
        result_status = str(result.get("status") or "")[:80] if isinstance(result, dict) else None
        # A caught failure is a successful Python return, not a successful job.
        if status == "executed" and result_status in {"failed", "missing", "degraded", "blocked"}:
            status = "business_degraded"
        row = {
            "job_id": str(event.job_id), "status": status,
            "observed_at": now.isoformat(),
            "scheduled_at": scheduled.isoformat() if scheduled else None,
            "finish_lateness_sec": round((now - scheduled).total_seconds(), 3) if scheduled else None,
            "result_status": result_status,
            "error_type": type(event.exception).__name__ if getattr(event, "exception", None) else None,
        }
        # Registered jobs are bounded, but keep the monitor bounded even if a
        # caller dynamically adds many IDs. Results expose owned scalar leaves.
        if row["job_id"] not in self._jobs and len(self._jobs) >= 100:
            self._jobs.pop(next(iter(self._jobs)))
        self._jobs[row["job_id"]] = row
        if status in {"error", "missed", "max_instances", "business_degraded"}:
            self._job_alerts.append(row)
        return dict(row)

    def snapshot(self, *, now: datetime | None = None) -> dict:
        current = local_clock(now) or datetime.now()
        today = current.date()
        calendar_known = self._calendar is not None and self._calendar[0] == today
        trade_day = self._calendar[1] if calendar_known else None
        if today.weekday() >= 5 or is_official_closed_day(today):
            trade_day, calendar_known = False, True
        active_window = any(
            start <= current.time() < end
            for name, (start, end) in TRADE_SESSIONS.items()
            if name in {"pre_auction", "morning", "afternoon"}
        )
        expected = trade_day and active_window if calendar_known else None
        threshold = quote_round_continuity(None, current)["max_gap_sec"]
        last_at = self._quote_at
        today_quote = last_at is not None and last_at.date() == today
        tail = quote_round_continuity(last_at if today_quote else None, current)
        if not calendar_known:
            status = "calendar_unknown"
        elif not expected:
            status = "not_expected"
        elif not today_quote:
            boundary = max(self.started_at, datetime.combine(today, TRADE_SESSIONS["pre_auction"][0]))
            unobserved = quote_round_continuity(boundary, current)["active_gap_sec"] or 0.0
            status = "missing" if unobserved > threshold else "awaiting_first_round"
        elif last_at > current:
            status = "invalid_clock"
        elif tail["status"] == "gap":
            status = "stale"
        elif self._quote["quality_status"] != "ok":
            status = "degraded"
        else:
            status = "fresh"
        gaps = [dict(row) for row in self._gaps if row["observed_at"][:10] == today.isoformat()]
        alerts = [dict(row) for row in self._job_alerts if row["observed_at"][:10] == today.isoformat()]
        degraded = bool(gaps or alerts) or status in {"missing", "invalid_clock", "stale", "degraded"}
        quote = dict(self._quote)
        if "intercommit_continuity" in quote:
            quote["intercommit_continuity"] = dict(quote["intercommit_continuity"])
        return {
            "contract_version": "pipeline_observation_v1",
            "scope": "current_process_only", "started_at": self.started_at.isoformat(),
            "observed_at": current.isoformat(),
            "status": "degraded" if degraded else "unknown" if status in {
                "calendar_unknown", "awaiting_first_round",
            } else "ok",
            "historical_day_completeness": "not_certified",
            "business_success_requires_persisted_batch": True,
            "execution_gates_changed": False,
            "quote": {
                "status": status, "expected_now": expected,
                "calendar_known": calendar_known, "last_round": quote,
                "active_tail_sec": tail["active_gap_sec"], "max_gap_sec": threshold,
                "gap_events_today": gaps, "per_stock_clock_check_required": True,
            },
            "jobs": [dict(row) for row in self._jobs.values()],
            "job_alerts_today": alerts,
            "alert_retention_limit": 100,
        }
