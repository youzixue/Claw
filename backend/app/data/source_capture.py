"""Opt-in transport evidence, NOT reviewed daily truth or historical availability.

The normal sources do not enable this buffer. It retains bounded HTTP 200 entity
bodies before parsing/filtering; only an explicit research caller seals them after
collection. No DB, network, source-policy approval, ready publication or timers.
"""
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
import re

SCHEMA = "source_transport_capture_v1"
MAX_RESPONSES = 128
MAX_BODY_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 16 * 1024 * 1024
SOURCES = {
    "eastmoney_fund": "individual_fund_flow_v3_f124",
    "ths_kline": "v6_line_01_transport_v1",
}


@dataclass(frozen=True)
class CapturedResponse:
    source: str
    request_key: tuple
    raw: bytes
    received_at: datetime
    observed_at: datetime


class ResponseCapture:
    """One operation, owned immutable leaves; observe performs no filesystem I/O."""
    def __init__(self):
        self.started_at = datetime.now()
        self._responses = []
        self._bytes = 0
        self._seen = 0
        self._errors = Counter()
        self._sealed = False

    @property
    def responses(self):
        return tuple(self._responses)

    def observe(self, *, source, request_key, raw, received_at):
        self._seen += 1
        try:
            if self._sealed:
                raise ValueError("capture_already_sealed")
            if source not in SOURCES or type(request_key) is not tuple:
                raise ValueError("invalid_request_identity")
            if source == "eastmoney_fund":
                valid = (len(request_key) == 2 and all(type(v) is int and v > 0 for v in request_key))
            else:
                valid = (len(request_key) == 3 and isinstance(request_key[0], str)
                    and re.fullmatch(r"[0-9]{6}", request_key[0]) is not None
                    and isinstance(request_key[1], str)
                    and re.fullmatch(r"last\.js|[0-9]{4}\.js", request_key[1]) is not None
                    and type(request_key[2]) is int and request_key[2] > 0)
            if not valid:
                raise ValueError("invalid_request_identity")
            now = datetime.now()
            if (not isinstance(received_at, datetime) or received_at.tzinfo is not None
                    or not self.started_at <= received_at <= now):
                raise ValueError("invalid_transport_clock")
            if type(raw) is not bytes or not 0 < len(raw) <= MAX_BODY_BYTES:
                raise ValueError("invalid_body_size")
            if len(self._responses) >= MAX_RESPONSES or self._bytes + len(raw) > MAX_TOTAL_BYTES:
                raise ValueError("capture_budget_exceeded")
            self._responses.append(CapturedResponse(source, request_key, raw, received_at, now))
            self._bytes += len(raw)
        except ValueError as exc:
            # Reasons above are constants, never response contents/URL/credentials.
            self._errors[str(exc)] += 1

    def seal(self, archive_root, *, operation_status, error_type=None):
        """Seal now, once, on a worker. A receipt is inventory, never model ready.

        HTTPX content is the decoded HTTP entity body, not compressed wire bytes.
        The MaterialArchive envelope has its own actual seal-time clocks; captured
        receipt clocks are separate and are not backdated into that envelope.
        """
        from app.promotion.modeling.daily_materials import MaterialArchive, encode
        if self._sealed:
            raise ValueError("capture_already_sealed")
        if operation_status not in {"returned", "failed", "cancelled"}:
            raise ValueError("invalid_operation_status")
        if error_type is not None and (not isinstance(error_type, str)
                or re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,99}", error_type) is None):
            raise ValueError("invalid_error_type")
        if (operation_status == "returned") != (error_type is None):
            raise ValueError("inconsistent_operation_status")
        self._sealed = True
        archive = MaterialArchive(archive_root)
        refs = []
        for observation in self.responses:
            envelope = archive.archive(observation.raw, {
                "source": observation.source,
                "source_version": SOURCES[observation.source],
                # Capture says nothing about dates inside a year file or finality.
                "provenance": "observed_now_transport_unreviewed",
            })
            refs.append({
                "source": observation.source, "source_version": SOURCES[observation.source],
                "request_key": list(observation.request_key), "http_status": 200,
                "body_representation": "httpx_decoded_entity_body",
                "received_at": observation.received_at.isoformat(),
                "observed_at": observation.observed_at.isoformat(), "envelope_ref": envelope,
            })
        report = {
            "schema_version": SCHEMA, "started_at": self.started_at.isoformat(),
            "sealed_at": datetime.now().isoformat(), "operation_status": operation_status,
            "error_type": error_type, "response_count_seen": self._seen,
            "response_count_retained": len(refs), "retained_body_bytes": self._bytes,
            "capture_errors": dict(self._errors), "responses": refs,
            "status": "captured_unverified" if refs and not self._errors else "incomplete_or_empty",
            "formal_ready": False, "historical_first_known_verified": False,
            "universe_complete": None, "source_published_at": None,
            "scope": "Only HTTP 200 bodies; transport/status failures and cookie bootstrap bodies are not retained.",
        }
        # No ready directory or receipt: callers get an exact content-addressed ref.
        report_ref = archive.put(encode(report))
        return {"report_ref": report_ref, "report": report}


def observe_response(capture, *, source, request_key, response, received_at):
    """Do not let optional capture alter parsing, pacing, retries or cancellation."""
    if capture is None:
        return
    try:
        if response.status_code != 200:
            if isinstance(capture, ResponseCapture):
                capture._errors["non_200_body_not_retained"] += 1
            return
        capture.observe(source=source, request_key=request_key,
                        raw=response.content, received_at=received_at)
    except Exception as exc:
        # Audit an optional observer failure without disclosing arbitrary messages.
        if isinstance(capture, ResponseCapture):
            capture._errors["observer_failure"] += 1
        from loguru import logger
        logger.warning("[source_capture] observation failed ({})", type(exc).__name__)
