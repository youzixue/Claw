"""Forward-only, file-backed research observer. No business database or execution calls.

Callbacks copy bounded owned leaves only. One bounded queue and one worker own
all histories, pure evaluation, labels and immutable publication. Missing capture
is never reconstructed from current market state.
"""
import asyncio
from collections import Counter, OrderedDict, deque
from datetime import date, datetime, timedelta
import hashlib
import gzip
import stat
import zlib
import json
import math
import os
from pathlib import Path
import queue
import sys
import heapq
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_OUTPUT = ROOT / "outputs/strategy_candidate_shadow"
QUEUE_SIZE = 8192
MAX_QUEUE_BYTES = 128 * 1024 * 1024
MAX_LABEL_STATES = 64
LABEL_SAMPLING_POLICY = "first_state_per_episode_v2_with_confirmation_freshness"
MAX_QUOTES = 6000
MAX_STREAMS = 4096
MAX_HISTORY_CODES = 6000
MAX_HISTORY_ROWS = 40
MAX_PENDING = 8192
BATCH_SIZE = 64
MAX_REPORT_FILES = 4096
MAX_REPORT_TOTAL_BYTES = 64 * 1024 * 1024
MAX_REPORT_DECODED_BYTES = 64 * 1024 * 1024
MAX_REPORT_FILE_BYTES = 16 * 1024 * 1024
STORAGE_ALLOCATION_SLACK_BYTES = 1024 * 1024
MAX_REPORT_SECONDS = 2.0
MAX_REPORT_DIRECTORY_ENTRIES = 20000
STOP_TIMEOUT_SECONDS = 5.0
MIN_DISK_RESERVE_BYTES = 5 * 1024 ** 3
SEALED_CORE_SHA256 = "4a98174565cb0e80c70db4baf4dc69a48a033c1f10ae90213484860fcb892934"
QUOTE_KEYS = ("code", "price", "prev_close", "avg_price", "ask1_price", "ask1_volume",
              "limit_up", "source_quote_at", "received_at", "observed_at", "updated_at",
              "committed_at", "volume_ratio", "amount", "bid1_price", "bid1_volume",
              "quote_round_id")
FRAME_KEYS = ("route", "observed_at", "account_id", "account_name", "production_version",
              "code", "name", "evidence_ref", "scan_id", "stage", "reason",
              "original_candidate", "original_confirmed", "original_gate",
              "relative_strength_pct", "sector_relative_strength_pct", "probability",
              "producer_reported_at", "source_persistence", "episode_id", "source_contract")
_runtime = None


def _now():
    return datetime.now()


def _clock(value):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    return value if type(value) is datetime and value.tzinfo is None else None


def _leaf(value, depth=0, budget=None):
    # Bound projection work before allocating the tree, independently of queue admission.
    budget = [0] if budget is None else budget
    budget[0] += 128 + (4 * len(value) if type(value) is str else 0)
    if budget[0] > 128 * 1024:
        raise ValueError("owned_tree_byte_budget")
    # Never inspect ORM objects, iterate arbitrary iterators, or stringify objects.
    if value is None or type(value) in (bool, int):
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("nonfinite_leaf")
        return value
    if type(value) is str:
        if len(value) > 2048:
            raise ValueError("oversized_leaf")
        return value
    if type(value) is datetime:
        if value.tzinfo is not None:
            raise ValueError("aware_clock")
        return value.isoformat()
    if depth < 3 and type(value) is dict and len(value) <= 64:
        if any(type(k) is not str or len(k) > 128 for k in value):
            raise ValueError("nonstring_key")
        budget[0] += sum(4 * len(k) + 64 for k in value)
        return {k: _leaf(v, depth + 1, budget) for k, v in value.items()}
    if depth < 3 and type(value) in (list, tuple) and len(value) <= 32:
        return [_leaf(v, depth + 1, budget) for v in value]
    raise ValueError("non_owned_or_oversized_leaf")


def _stock_code(value):
    return type(value) is str and len(value) == 6 and value.isascii() and value.isdigit()


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def _quote(record):
    if type(record) is not dict:
        raise ValueError("quote_not_owned_dict")
    return {k: _leaf(record[k]) for k in QUOTE_KEYS if k in record}


def capture_frame(packet: dict) -> None:
    runtime = _runtime
    if runtime is None or not runtime.active:
        return
    try:
        if runtime.queue.full():
            runtime.lost("queue_full", packet if type(packet) is dict else {})
            return
        if type(packet) is not dict:
            raise ValueError("packet_not_dict")
        owned = {k: _leaf(packet[k]) for k in FRAME_KEYS if k in packet}
        for key in ("gate_inputs", "rule_snapshot", "identities"):
            owned[key] = _leaf(packet.get(key, {}))
        owned["quote"] = _quote(packet.get("quote", {}))
        runtime.offer("frame", owned)
    except Exception:
        runtime.lost("capture_invalid", packet if type(packet) is dict else {})


def capture_quotes(records, *, observed_at, round_id) -> None:
    runtime = _runtime
    if runtime is None or not runtime.active:
        return
    try:
        if runtime.queue.full():
            runtime.lost("queue_full", {})
            return
        if type(records) not in (list, tuple) or len(records) > MAX_QUOTES:
            raise ValueError("quote_batch_unbounded")
        owned = {"records": [_quote(r) for r in records],
                 "observed_at": _leaf(observed_at), "round_id": _leaf(round_id)}
        runtime.offer("quotes", owned)
    except Exception:
        runtime.lost("quote_capture_invalid", {})


def candidate_shadow_active() -> bool:
    """Cheap pre-projection producer guard; no status allocation or I/O."""
    runtime = _runtime
    return runtime is not None and runtime.active


def candidate_shadow_status():
    runtime = _runtime
    if runtime is None:
        return {**_status_contract(), "active": False, "running": False,
                "status": "not_started", "production_permission": False}
    return runtime.status()


def _status_contract():
    from app.config.settings import settings
    return {"enabled": bool(getattr(settings, "PAPER_CANDIDATE_SHADOW_ENABLED", True)),
            "version": "research:strategy_candidate_experiment_20260924_v2",
            "storage_format": "compact_json_gzip_level1_v1",
            "storage_bytes": {"scope": "current_process", "encoded_total": 0,
                              "published_total": 0, "last_encoded": 0}}


async def start_candidate_shadow(*, output_dir=None, bindings=None):
    global _runtime
    if _runtime is not None and _runtime.thread.is_alive():
        return _runtime.status()
    # Bindings are caller-owned query-only snapshots. Never ensure/create accounts.
    runtime = _Runtime(Path(output_dir or DEFAULT_OUTPUT), _leaf(bindings or {}))
    _runtime = runtime
    runtime.thread.start()
    return runtime.status()


async def stop_candidate_shadow():
    runtime = _runtime
    if runtime is None:
        return candidate_shadow_status()
    runtime.active = False
    runtime.stopping.set()
    # Never put a potentially stuck join into the default executor: executor
    # shutdown could otherwise hold the original service hostage after cancellation.
    deadline = time.monotonic() + STOP_TIMEOUT_SECONDS
    while runtime.thread.is_alive():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            if not runtime.stop_timed_out:
                runtime.counts["stop_timeout"] += 1
                runtime.stop_timed_out = True
            return runtime.status()
        await asyncio.sleep(min(0.02, remaining))
    runtime.thread.join(timeout=0)
    return runtime.status()


def _owned_bytes(value):
    """Conservative owned heap accounting; shared strings intentionally overcount."""
    size = sys.getsizeof(value)
    if type(value) is dict:
        return size + sum(sys.getsizeof(k) + _owned_bytes(v) for k, v in value.items())
    if type(value) in (tuple, list):
        return size + sum(_owned_bytes(v) for v in value)
    return size


class _ByteQueue(queue.Queue):
    def __init__(self):
        super().__init__(maxsize=QUEUE_SIZE)
        self.queued_bytes = 0
        self.peak_bytes = 0
        self.peak_records = 0

    def put_nowait(self, item):
        weight = _owned_bytes(item)
        with self.not_full:
            if self._qsize() >= self.maxsize or self.queued_bytes + weight > MAX_QUEUE_BYTES:
                raise queue.Full
            self._put((*item, weight))
            self.queued_bytes += weight
            self.peak_bytes = max(self.peak_bytes, self.queued_bytes)
            self.peak_records = max(self.peak_records, self._qsize())
            self.unfinished_tasks += 1
            self.not_empty.notify()

    def _get(self):
        kind, payload, generation, weight = super()._get()
        self.queued_bytes -= weight
        return kind, payload, generation


def _encode_shadow(payload):
    # Preserve every leaf/record; only whitespace and the storage envelope change.
    content = (json.dumps(payload, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False) + "\n").encode()
    return gzip.compress(content, compresslevel=1, mtime=0)


def _open_shadow_directory(directory):
    """Open each component without following links (including day/parent links)."""
    absolute = Path(os.path.abspath(directory))
    descriptor = os.open(absolute.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in absolute.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _publish_shadow(content, directory, at):
    """Publish already encoded bytes atomically, never replace an existing name."""
    digest = hashlib.sha256(content).hexdigest()
    directory.mkdir(parents=True, exist_ok=True)
    descriptor = _open_shadow_directory(directory)
    name = f"paper-research-{at:%Y%m%dT%H%M%S%f}-{digest[:16]}.json.gz"
    temporary = f".{uuid.uuid4().hex}.tmp"
    created = False
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=descriptor)
        created = True
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        # A collision is fail-closed, including same-content collisions; no retry.
        os.link(temporary, name, src_dir_fd=descriptor, dst_dir_fd=descriptor,
                follow_symlinks=False)
    finally:
        try:
            if created:
                os.unlink(temporary, dir_fd=descriptor)
        finally:
            os.close(descriptor)
    return {"output": str(directory / name), "sha256": digest}


class _Runtime:
    def __init__(self, directory, bindings):
        self.directory, self.bindings = directory, _leaf(bindings)
        self.session_id = uuid.uuid4().hex
        self.queue = _ByteQueue()
        self.stopping = threading.Event()
        self.stop_timed_out = False
        self.drain_complete = False
        self.disk_budget = {"blocked": False, "reason": None, "available_bytes": None,
                            "reserve_bytes": MIN_DISK_RESERVE_BYTES, "next_write_bytes": None}
        self.active = True
        self.thread = threading.Thread(target=self.run, name="candidate-shadow-research", daemon=True)
        self.counts = Counter()
        self.generation = 0
        self.applied_generation = 0
        self.last_loss = {}
        self.watermarks = {"captured": None, "evaluated": None, "durable": None}
        self.history = OrderedDict()
        self.streams = OrderedDict()
        self.pending = OrderedDict()
        self.batch = []
        self.started_at = _now().isoformat()
        self.last_error = None
        self.core = None
        # Read-only startup policy capture. Never derive execution TTL from a
        # research window and never ensure accounts or query the business DB.
        from app.paper.account_policy import ROUTE_ACCOUNT_NAMES, challenger_execution_policy
        short_routes = {"challenger_a": "A2", "challenger_b": "B2", "challenger_c": "C2",
                        "challenger_d": "D2", "challenger_f2": "F2"}
        self.execution_contracts = {}
        for route_id, account_name in ROUTE_ACCOUNT_NAMES.items():
            route = short_routes.get(account_name)
            binding = self.bindings.get(account_name, {})
            if route is None or type(binding) is not dict:
                continue
            try:
                ttl = challenger_execution_policy(route_id)["max_execution_delay_sec"]
                if (type(ttl) not in (int, float) or not math.isfinite(ttl) or ttl <= 0
                        or type(binding.get("account_id")) is not int or binding["account_id"] <= 0
                        or type(binding.get("strategy_version")) is not str
                        or not binding["strategy_version"]):
                    continue
                self.execution_contracts[route] = {
                    "schema": "candidate_shadow_execution_freshness_v1", "route": route,
                    "account_name": account_name, "account_id": binding["account_id"],
                    "execution_strategy_version": binding["strategy_version"],
                    "max_execution_delay_sec": ttl, "policy_observed_at": _now().isoformat()}
            except (KeyError, TypeError, ValueError, OverflowError):
                self.counts["execution_policy_unproven"] += 1

    def status(self):
        alive = self.thread.is_alive()
        stopping = self.stopping.is_set()
        state = ("drain_incomplete" if self.stop_timed_out else "stopping") if stopping and alive else (
            "running" if self.active and alive else "stopped" if self.drain_complete else "stopped_incomplete")
        return {**_status_contract(), "active": self.active, "running": self.active and alive,
                "status": state, "worker_alive": alive, "stop_timed_out": self.stop_timed_out,
                "drain_complete": self.drain_complete,
                "drain_incomplete": stopping and not self.drain_complete,
                "drain_status": "complete" if self.drain_complete else "unknown",
                "stop_timeout_seconds": STOP_TIMEOUT_SECONDS,
                "disk_budget": dict(self.disk_budget),
                "storage_bytes": {"scope": "current_process",
                                  "encoded_total": self.counts["storage_encoded_bytes"],
                                  "published_total": self.counts["storage_published_bytes"],
                                  "last_encoded": self.counts["storage_last_encoded_bytes"]},
                "session_id": self.session_id, "started_at": self.started_at,
                "queue_size": self.queue.qsize(), "queue_capacity": QUEUE_SIZE,
                "queued_bytes": self.queue.queued_bytes, "queue_byte_capacity": MAX_QUEUE_BYTES,
                "peak_queue_bytes": self.queue.peak_bytes, "peak_queue_records": self.queue.peak_records,
                "label_sampling_policy": LABEL_SAMPLING_POLICY,
                "counts": dict(self.counts), "watermarks": dict(self.watermarks),
                "gap_generation": self.generation, "last_loss": dict(self.last_loss),
                "core_sha256": getattr(self, "core_sha", None),
                "sealed_core_match": getattr(self, "core_sha", None) == SEALED_CORE_SHA256,
                "last_error": self.last_error, "production_permission": False,
                "reference_is_fill": False, "net_profit": None}

    def lost(self, reason, packet):
        self.counts[reason] += 1
        self.generation += 1
        self.last_loss = {"reason": reason, "at": _now().isoformat(),
                          "route": packet.get("route")[:128] if type(packet.get("route")) is str else None,
                          "scan_id": packet.get("scan_id")[:128] if type(packet.get("scan_id")) is str else None,
                          "missing_count": self.counts[reason]}

    def offer(self, kind, payload):
        payload["capture_at"] = _now().isoformat()
        observed = _clock(payload.get("observed_at"))
        captured = _clock(payload["capture_at"])
        if observed is None or not (observed <= captured and observed.date() == captured.date()
                                    and (captured - observed).total_seconds() <= 180):
            self.lost("capture_clock_invalid", payload)
            return
        try:
            self.queue.put_nowait((kind, payload, self.generation))
            self.counts["captured_" + kind] += 1
            self.watermarks["captured"] = _now().isoformat()
        except queue.Full:
            self.lost("queue_full", payload)

    def emit(self, record):
        record.update(session_id=self.session_id, recorded_at=_now().isoformat())
        self.batch.append(record)
        if len(self.batch) >= BATCH_SIZE:
            self.flush()

    def boundary(self):
        if self.applied_generation == self.generation:
            return
        self.emit({"kind": "gap", "trade_date": _now().date().isoformat(),
                   "generation_from": self.applied_generation, "generation_to": self.generation,
                   "last_loss": dict(self.last_loss), "affected_scope": "all_active_streams",
                   "missing_count": self.generation - self.applied_generation})
        self.history.clear()
        self.streams.clear()
        for pending in self.pending.values():
            pending["gap"] = True
        self.applied_generation = self.generation

    def disk_allows(self, target, content):
        if self.disk_budget["blocked"]:
            return False
        reason = None
        try:
            # The exact encoded bytes will be written once; allow allocation slack.
            size = len(content) + STORAGE_ALLOCATION_SLACK_BYTES
            self.disk_budget["next_write_bytes"] = size
            probe = target.absolute()
            while True:
                try:
                    stats = os.statvfs(probe)
                    break
                except FileNotFoundError:
                    if probe.parent == probe:
                        raise
                    probe = probe.parent
            available = stats.f_bavail * stats.f_frsize
            self.disk_budget["available_bytes"] = available
            if available < MIN_DISK_RESERVE_BYTES + size:
                reason = "disk_reserve_insufficient"
        except Exception as exc:
            reason = "disk_space_query_failed"
            self.last_error = type(exc).__name__
        if reason is None:
            return True
        self.disk_budget.update(blocked=True, reason=reason, checked_at=_now().isoformat())
        self.active = False
        self.stopping.set()
        self.counts["disk_budget_censored_anchors"] += len(self.pending)
        self.counts["disk_budget_censored_horizons"] += sum(len(p["targets"]) for p in self.pending.values())
        self.lost("disk_budget", {})
        # Do not attempt even a gap/status file after the guard trips. Existing files
        # and business DB are untouched; status carries undurable/censor evidence.
        return False

    def flush(self):
        if not self.batch:
            return
        records, self.batch = self.batch, []
        groups = {}
        for record in records:
            groups.setdefault(record["trade_date"], []).append(record)
        for day, rows in groups.items():
            try:
                payload = {"schema": "candidate_shadow_runtime_v1", "session_id": self.session_id,
                           "core_sha256": self.core_sha, "records": rows,
                           "production_permission": False}
                content = _encode_shadow(payload)
                self.counts["storage_encoded_bytes"] += len(content)
                self.counts["storage_last_encoded_bytes"] = len(content)
                if not self.disk_allows(self.directory / day, content):
                    self.counts["undurable_records"] += len(rows)
                    self.counts["disk_budget_undurable_records"] += len(rows)
                    continue
                _publish_shadow(content, self.directory / day, _now())
                self.counts["storage_published_bytes"] += len(content)
                self.counts["durable_records"] += len(rows)
                self.watermarks["durable"] = rows[-1]["recorded_at"]
            except Exception as exc:
                self.last_error = type(exc).__name__
                self.counts["undurable_records"] += len(rows)
                self.lost("write_failed", {})
                # No retry queue: bounded gap metadata is persisted on recovery.

    def run(self):
        try:
            from app.paper import intraday_route_research as core
            self.core = core
            self.core_sha = hashlib.sha256(Path(core.__file__).read_bytes()).hexdigest()
            if self.core_sha != SEALED_CORE_SHA256:
                raise ValueError("sealed_core_source_changed")
            self.emit({"kind": "restart_boundary", "trade_date": _now().date().isoformat(),
                       "missing_count": None, "replay": False, "warmup_seconds": 360,
                       "bindings": self.bindings, "execution_contracts": self.execution_contracts,
                       "previous_session_continuity": "unknown"})
            self.flush()
            last_flush = time.monotonic()
            while not self.stopping.is_set() or not self.queue.empty():
                self.boundary()
                try:
                    kind, payload, generation = self.queue.get(timeout=0.2)
                except queue.Empty:
                    kind = None
                if kind:
                    try:
                        if generation != self.applied_generation:
                            self.counts["gap_discarded_packets"] += 1
                            self.emit({"kind": "gap_discard", "trade_date": _now().date().isoformat(),
                                       "route": payload.get("route"), "scan_id": payload.get("scan_id"),
                                       "missing_count": 1})
                        elif kind == "quotes":
                            self.quotes(payload)
                        else:
                            self.frame(payload)
                    except Exception as exc:
                        self.last_error = type(exc).__name__
                        self.lost("worker_invalid", payload)
                    finally:
                        self.queue.task_done()
                if len(self.batch) >= BATCH_SIZE or time.monotonic() - last_flush >= 1:
                    self.flush()
                    last_flush = time.monotonic()
            for item in self.pending.values():
                self.emit({"kind": "label_censored", "trade_date": item["day"],
                           "frame_id": item["id"], "route": item["route"],
                           "remaining_horizons": list(item["targets"]),
                           "reason": "disk_budget" if self.disk_budget["blocked"] else "stop_or_restart"})
                if len(self.batch) >= BATCH_SIZE:
                    self.flush()
            self.emit({"kind": "stop_boundary", "trade_date": _now().date().isoformat(),
                       "counts": dict(self.counts), "watermarks": dict(self.watermarks)})
            self.flush()
            self.drain_complete = not self.counts["undurable_records"] and self.queue.empty()
        except Exception as exc:
            self.last_error = type(exc).__name__
            self.counts["worker_fatal"] += 1
        finally:
            self.active = False

    def quote_visibility(self, quote, at):
        q = dict(quote)
        if "observed_at" not in q or q["observed_at"] is None:
            source, received, updated = (_clock(q.get(k)) for k in
                                         ("source_quote_at", "received_at", "updated_at"))
            if all((source, received, updated, at)) and source <= received <= updated <= at:
                q["observed_at"] = updated.isoformat()
                q["quote_observation_basis"] = "nominal_updated_visible_by_capture"
        return q

    def observed_quote(self, quote, at, basis):
        q = self.quote_visibility(quote, at)
        original = _clock(q.get("observed_at"))
        source, received = _clock(q.get("source_quote_at")), _clock(q.get("received_at"))
        if (all((original, source, received, at)) and source <= received <= original <= at
                and self.core._session(original) == self.core._session(source)):
            q["quote_observed_at"] = quote.get("observed_at")
            q["quote_visibility_at"] = q["observed_at"]
            q["observed_at"] = at.isoformat()
            q["experiment_observation_basis"] = basis
        return q

    def valid_quote(self, q, at):
        clocks = [_clock(q.get(k)) for k in ("source_quote_at", "received_at", "observed_at")]
        if not at or not all(clocks):
            return False
        source, received, observed = clocks
        if not (source <= received <= observed <= at and source.date() == at.date()
                and self.core._session(source)
                and self.core._session(source) == self.core._session(received) == self.core._session(observed)
                and (observed - source).total_seconds() <= 180):
            return False
        for key in ("updated_at", "committed_at"):
            if key in q and not (clocks[1] <= (_clock(q[key]) or datetime.min) <= observed):
                return False
        return all(type(q.get(k)) in (int, float) and q[k] > 0 for k in ("price", "prev_close"))

    def quotes(self, payload):
        at = _clock(payload["observed_at"])
        if at is None or at > _now():
            raise ValueError("quote_batch_clock")
        accepted = {}
        for raw in payload["records"]:
            q = self.observed_quote(raw, at, "quote_payload_actual_publish")
            code = q.get("code")
            if type(code) is not str or not self.valid_quote(q, at):
                self.counts["invalid_quotes"] += 1
                if type(code) is str:
                    self.history.pop(code, None)
                    for item in self.pending.values():
                        if item["code"] == code:
                            item["gap"] = True
                continue
            source = _clock(q["source_quote_at"])
            rows = self.history.setdefault(code, deque(maxlen=MAX_HISTORY_ROWS))
            if rows and source <= _clock(rows[-1]["source_quote_at"]):
                self.counts["duplicate_or_reversed_quotes"] += 1
                material = lambda row: {k: v for k, v in row.items() if k not in
                                        ("received_at", "observed_at", "updated_at", "committed_at",
                                         "quote_round_id", "quote_observation_basis", "quote_observed_at",
                                         "quote_visibility_at", "experiment_observation_basis")}
                if source == _clock(rows[-1]["source_quote_at"]) and material(q) == material(rows[-1]):
                    continue
                self.emit({"kind": "quote_gap", "trade_date": at.date().isoformat(),
                           "code": code, "reason": "source_conflict_or_reversal",
                           "round_id": payload["round_id"], "missing_count": None})
                # Conflicting same-source or out-of-order delivery cannot form a path.
                rows.clear()
                for item in self.pending.values():
                    if item["code"] == code:
                        item["gap"] = True
                continue
            if len(rows) == MAX_HISTORY_ROWS and (at - _clock(rows[0]["observed_at"])).total_seconds() <= 360:
                self.counts["history_row_capacity"] += 1
                self.emit({"kind": "quote_gap", "trade_date": at.date().isoformat(),
                           "code": code, "reason": "history_row_capacity", "missing_count": 1})
            rows.append(q)
            while rows and (at - _clock(rows[0]["observed_at"])).total_seconds() > 360:
                rows.popleft()
            self.history.move_to_end(code)
            accepted[code] = q
        for code in list(self.history):
            rows = self.history[code]
            if not rows or (at - _clock(rows[-1]["observed_at"])).total_seconds() > 360:
                del self.history[code]
        while len(self.history) > MAX_HISTORY_CODES:
            self.history.popitem(last=False)
            self.counts["history_evicted"] += 1
        self.labels(accepted, at)

    def b2_scan_guard(self, candidate, samples, at, result):
        """Additional provenance clock check on the SAME sealed-core qualifying tail.

        Uses sealed normalization/gate/offer helpers, never production strategy
        evaluation. No worker/capture/recorded clock supplies persistence duration.
        """
        policy = self.core.RouteResearchPolicy("research:b2_2x30_v1", 2, 30, 75)
        rows, _, fatal = self.core._experiment_rows(candidate, samples, at, policy)
        guard = {"version": "producer_scan_2x30_evidence_v1", "status": "not_applicable",
                 "value": None, "reason": "no_qualifying_original_gate_streak",
                 "min_samples": 2, "min_persistence_sec": 30, "max_sample_gap_sec": 75,
                 "clock_jitter_sec": 0, "sample_count": 0, "evidence_refs": []}
        if fatal:
            guard.update(status="unknown", reason="sealed_sample_path_invalid")
            return guard
        frozen = _clock(candidate["frozen_at"])
        streak = []
        for row in rows:
            if row["observed_at"] < frozen:
                continue
            if (self.core._experiment_gate(candidate, row) is not True
                    or self.core._experiment_offer(row) is not True):
                streak = []
                continue
            if streak and (
                self.core._session(row["source_quote_at"]) != self.core._session(streak[-1]["source_quote_at"])
                or (row["source_quote_at"]-streak[-1]["source_quote_at"]).total_seconds() > 75
                or not 0 < (row["observed_at"]-streak[-1]["observed_at"]).total_seconds() <= 75
            ):
                streak = []
            streak.append(row)
        if not streak:
            return guard
        guard["sample_count"] = len(streak)
        guard["evidence_refs"] = [row.get("original_gate_ref") for row in streak]
        reported = [_clock(row.get("producer_reported_at")) for row in streak]
        if any(clock is None or not row["received_at"] <= clock <= row["observed_at"]
               or clock.date() != row["source_quote_at"].date()
               or self.core._session(clock) != self.core._session(row["source_quote_at"])
               for row, clock in zip(streak, reported)):
            guard.update(status="unknown", reason="producer_reported_clock_unproven")
        else:
            gaps = [(b-a).total_seconds() for a, b in zip(reported, reported[1:])]
            span = (reported[-1]-reported[0]).total_seconds()
            guard.update(producer_reported_span_sec=span,
                         source_span_sec=(streak[-1]["source_quote_at"]-streak[0]["source_quote_at"]).total_seconds(),
                         actual_observed_span_sec=(streak[-1]["observed_at"]-streak[0]["observed_at"]).total_seconds())
            if any(not 0 < gap <= 75 for gap in gaps):
                guard.update(status="unknown", reason="producer_reported_gap_or_reversal")
            elif len(streak) < 2 or span < 30:
                guard.update(status="control", value=False, reason="producer_reported_persistence_not_proven")
            else:
                guard.update(status="observed", value=True, reason="original_scan_and_actual_observation_both_proven")
        return guard

    def frame(self, p):
        p = self.core._owned(p)
        at = _clock(p.get("observed_at"))
        if not at or at > _now():
            raise ValueError("predicate_clock")
        route = p.get("route")
        ids = p["identities"]
        expected = self.core.STRATEGY_CANDIDATE_ACCOUNTS.get(route)
        binding = self.bindings.get(expected, {}) if expected else {}
        effective_account_id = p.get("account_id")
        if effective_account_id is None and route != "C3" and p.get("account_name") == expected:
            effective_account_id = binding.get("account_id")
        visible_quote = self.observed_quote(p["quote"], at, "producer_actual_predicate_read")
        identity_ok = (route in self.core.STRATEGY_CANDIDATE_ROUTES
                       and type(p.get("scan_id")) is str and bool(p["scan_id"])
                       and "account_name" in p and "account_id" in p
                       and p.get("account_name") == expected
                       and (p.get("account_id") is None if route == "C3" else
                            type(effective_account_id) is int
                            and effective_account_id == binding.get("account_id")
                            and bool(binding.get("strategy_version"))))
        if p.get("code") in ("", "MARKET"):
            stage = p.get("stage")
            receipt_type = ("scan_completed" if stage in ("scan_complete", "scan_completed")
                            else "not_scanned" if stage == "not_scanned" else "scan_issue")
            inputs = p["gate_inputs"]
            counts = {key: value for key, value in inputs.items()
                      if (key.endswith("_count") or key.endswith("_counts"))
                      and type(value) in (int, float)}
            if type(inputs.get("counts")) is dict:
                counts.update({key: value for key, value in inputs["counts"].items()
                               if type(value) in (int, float)})
            self.emit({"kind": "scan_receipt", "trade_date": at.date().isoformat(),
                       "receipt_type": receipt_type, "route": route, "stage": stage,
                       "reason": p.get("reason"), "scan_id": p.get("scan_id"),
                       "coverage_counts": counts, "stock_denominator": False,
                       "predicate_asof": at.isoformat(), "capture_input": p,
                       "input_sha256": _hash(p), "execution_binding_valid": identity_ok,
                       "execution_strategy_version": binding.get("strategy_version"),
                       "production_permission": False})
            self.counts["scan_receipts"] += 1
            return
        if not _stock_code(p.get("code")):
            self.emit({"kind": "invalid_identity", "trade_date": at.date().isoformat(),
                       "route": route, "code": p.get("code"), "stage": p.get("stage"),
                       "reason": "stock_code_not_six_ascii_digits",
                       "producer_reason": p.get("reason"), "scan_id": p.get("scan_id"),
                       "predicate_asof": at.isoformat(), "capture_input": p,
                       "input_sha256": _hash(p), "stock_denominator": False,
                       "production_permission": False})
            self.counts["invalid_identity"] += 1
            return
        episode = p.get("episode_id") or p.get("scan_id")
        cohort = p["gate_inputs"].get("prediction_run_id") if route in ("B", "C") else None
        key = _hash([at.date().isoformat(), route, effective_account_id, p.get("production_version"),
                     p.get("code"), episode, cohort, ids.get("round_id"), self.session_id])
        state = self.streams.get(key)
        if state is None:
            state = {"frozen": ids.get("candidate_frozen_at") or at.isoformat(),
                     "rows": deque(maxlen=MAX_HISTORY_ROWS), "last_predicate": None,
                     "baseline": None, "baseline_at": None, "context_floor": None,
                     "confirmation_invalidated_at": None,
                     "label_states": {}, "evaluation_cache": None,
                     "fingerprints": deque(maxlen=MAX_HISTORY_ROWS)}
            self.streams[key] = state
        fingerprint = _hash({k: v for k, v in p.items() if k != "capture_at"})
        if fingerprint in state["fingerprints"]:
            self.counts["duplicate_frames"] += 1
            return
        state["fingerprints"].append(fingerprint)
        if p.get("stage") == "reset":
            state["rows"].clear()
            state["baseline"], state["baseline_at"] = None, None
            state["context_floor"] = at
            state["confirmation_invalidated_at"] = at.isoformat()
            self.emit({"kind": "gap", "trade_date": at.date().isoformat(), "route": route,
                       "candidate_id": key, "scan_id": p.get("scan_id"),
                       "reason": "producer_reset", "missing_count": None})
        if type(p.get("original_confirmed")) is bool:
            state["baseline"] = p["original_confirmed"]
            # Membership-only follow-ups are not new confirmation evidence.
            # Retain a proven clock within this stream, never invent one.
            if not p["original_confirmed"] or "original_confirmed_at" in ids:
                state["baseline_at"] = ids.get("original_confirmed_at") if p["original_confirmed"] else None
        c = {k: p.get(k) for k in ("route", "code", "account_id", "account_name",
                                  "production_version", "evidence_ref", "original_candidate",
                                  "original_confirmed")}
        c["account_id"] = effective_account_id
        c["execution_strategy_version"] = binding.get("strategy_version")
        if state["confirmation_invalidated_at"] is not None:
            c["confirmation_invalidated_at"] = state["confirmation_invalidated_at"]
        c["original_confirmed"] = state["baseline"]
        if state["baseline_at"] is not None:
            c["original_confirmed_at"] = state["baseline_at"]
        if "source_contract" in p:
            c["source_contract"] = p["source_contract"]
        if "probability" in p:
            c["probability"] = p["probability"]
        c.update(candidate_id=key, trade_date=at.date().isoformat(), frozen_at=state["frozen"],
                 gate_inputs=dict(p["gate_inputs"], rule_snapshot=p["rule_snapshot"]),
                 gate_ref=p.get("evidence_ref"))
        for field in ("original_confirmed_at", "context_start_at", "pool_identity", "mainline_identity",
                      "highboard_identity", "broken_board_identity", "probability",
                      "round_id", "previous_round_id", "cancelled_at", "source_contract"):
            if field in ids:
                c[field] = ids[field]
        q = dict(visible_quote)
        q.update(candidate_id=key, sample_id=fingerprint, evidence_ref=p.get("evidence_ref"),
                 account_id=effective_account_id, account_name=p.get("account_name"),
                 original_gate=p.get("original_gate"), original_gate_ref=p.get("evidence_ref"),
                 predicate_observed_at=at.isoformat(), predicate_evidence_ref=p.get("evidence_ref"),
                 producer_reported_at=p.get("producer_reported_at"))
        for field in ("relative_strength_pct", "sector_relative_strength_pct"):
            q[field] = p.get(field)
        invalid_order = state["last_predicate"] is not None and at < state["last_predicate"]
        state["last_predicate"] = at
        if len(state["rows"]) == MAX_HISTORY_ROWS:
            self.counts["predicate_history_capacity"] += 1
            self.emit({"kind": "gap", "trade_date": c["trade_date"], "route": route,
                       "candidate_id": key, "reason": "predicate_history_capacity", "missing_count": 1})
        source_conflict = False
        if self.valid_quote(visible_quote, at):
            previous = next((row for row in state["rows"]
                             if row.get("source_quote_at") == q.get("source_quote_at")), None)
            if previous is not None:
                material = ("price", "prev_close", "avg_price", "ask1_price", "ask1_volume", "limit_up")
                source_conflict = any(previous.get(k) != q.get(k) for k in material)
                if type(previous.get("original_gate")) is bool:
                    if type(q.get("original_gate")) is bool and q["original_gate"] != previous["original_gate"]:
                        source_conflict = True
                    q["original_gate"] = previous["original_gate"]
                    q["original_gate_ref"] = previous["original_gate_ref"]
                    q["predicate_evidence_ref"] = previous["original_gate_ref"]
                    # Retain earliest visible source frame; current predicate clock
                    # is separate and can carry later original formal confirmation.
                    q["observed_at"] = previous["observed_at"]
                    q["received_at"] = previous["received_at"]
                    q["producer_reported_at"] = previous.get("producer_reported_at")
                    if p.get("original_gate") is None and p.get("original_confirmed") is None:
                        q["predicate_observed_at"] = previous["predicate_observed_at"]
                for field in ("relative_strength_pct", "sector_relative_strength_pct"):
                    if q.get(field) is None:
                        q[field] = previous.get(field)
                state["rows"] = deque((q if row is previous else row for row in state["rows"]),
                                      maxlen=MAX_HISTORY_ROWS)
                self.counts["same_source_stage_frames"] += 1
            else:
                state["rows"].append(q)
            rows = list(state["rows"])
        else:
            # Pure candidate enumeration carries no quote claim. Keep its own
            # unknown result, but do not erase the last genuine source samples.
            # Explicit quote failures, malformed quotes and predicate failures
            # still break the segment; no metadata row enters persistence history.
            metadata_only = (p.get("stage") == "candidate_observed" and p["quote"] == {}
                             and p.get("original_candidate") is True
                             and p.get("original_gate") is None
                             and p.get("original_confirmed") is None
                             and identity_ok and not invalid_order)
            if metadata_only:
                self.counts["metadata_quote_absent_preserved"] += 1
            else:
                state["rows"].clear()
            rows = [q]
            self.counts["invalid_frame_quote"] += 1
        if route in ("A", "C2", "C3", "F", "F2"):
            first_source = _clock(rows[0].get("source_quote_at"))
            for old in self.history.get(p.get("code"), ()):
                source = _clock(old.get("source_quote_at"))
                if (source and first_source and source < first_source
                        and _clock(old["observed_at"]) <= at
                        and (state["context_floor"] is None or _clock(old["observed_at"]) >= state["context_floor"])
                        and (at - source).total_seconds() <= 300):
                    rows.insert(0, dict(old, candidate_id=key, sample_id=_hash(old),
                                        evidence_ref="quote:" + _hash(old), account_id=c["account_id"],
                                        account_name=c["account_name"], sample_role="feature_history"))
        if "context_start_at" not in c:
            # Actual supplied quote visibility may precede its predicate. This is
            # explicit observed context, never a backdated candidate/persistence.
            visible = [_clock(row.get("observed_at")) for row in rows]
            visible = [clock for clock in visible if clock and clock.date() == at.date() and clock <= at]
            frozen = _clock(c["frozen_at"])
            if visible and frozen and min(visible) <= frozen:
                c["context_start_at"] = min(visible).isoformat()
        # Redundant observer stages carry no new predicate. Reuse the sealed
        # result *at its original evaluated clock*, explicitly linking evidence;
        # never pretend that reuse is a fresh gate evaluation or confirmation.
        semantic = _hash({
            "candidate": {k: v for k, v in c.items() if k not in ("evidence_ref", "gate_ref")},
            "samples": [{k: v for k, v in row.items() if k not in (
                "sample_id", "evidence_ref", "predicate_observed_at", "predicate_evidence_ref")}
                for row in rows],
        })
        cache = state["evaluation_cache"]
        reuse = (cache is not None and cache["semantic"] == semantic
                 and p.get("stage") in ("confirmation_sample", "observation", "post_confirm_observation")
                 and p.get("original_gate") is None and p.get("original_confirmed") is None
                 and not invalid_order and not source_conflict
                 and self.valid_quote(visible_quote, at)
                 and 0 <= (at - _clock(visible_quote["source_quote_at"])).total_seconds() <= 180)
        evaluation = {"mode": "sealed_evaluate", "from_frame_id": None}
        if reuse:
            c, rows = cache["candidate"], cache["samples"]
            result = self.core._owned(cache["result"])
            evaluation = {"mode": "same_source_stage_reuse", "from_frame_id": cache["frame_id"],
                          "evaluated_at": result.get("evaluated_at"), "as_of": result.get("as_of")}
            self.counts["core_evaluation_reused"] += 1
        else:
            # The v2 entry computes v1 once. Do not separately call v1 here.
            result = self.core.evaluate_strategy_candidate_experiment_v2(
                c, rows, as_of=at, execution_contract=self.execution_contracts.get(route))
            # Runtime persists one copy of each arm; core's opt-in aliases remain
            # available to standalone callers without bloating every frame.
            for alias in ("research_v2", "v1_diagnostic", "research_v2_scope"):
                result.pop(alias, None)
            self.counts["core_evaluated"] += 1
            self.counts["v2_evaluated"] += 1
            if route == "B2":
                guard = self.b2_scan_guard(c, rows, at, result)
                result["sealed_candidate"] = dict(result["candidate"])
                result["producer_scan_guard"] = guard
                if guard["status"] in ("unknown", "control"):
                    result["candidate"] = {"status": guard["status"], "value": guard["value"],
                                           "reason": guard["reason"], "first_event": None}
        if not identity_ok or invalid_order or source_conflict:
            result["candidate"] = {"status": "unknown", "value": None, "first_event": None,
                                   "reason": "execution_binding_unproven" if not identity_ok else
                                   "same_source_gate_or_quote_conflict" if source_conflict else "predicate_clock_reversed"}
        if source_conflict:
            state["rows"].clear()
        if p.get("original_confirmed") is True and "original_confirmed_at" in ids:
            # Validate with the frozen core before retaining proof. In particular,
            # an explicit future/invalid clock must not mature on a later frame.
            state["baseline_at"] = (c.get("original_confirmed_at")
                                    if identity_ok and not invalid_order
                                    and result["baseline"]["value"] is True else None)
        if not reuse:
            state["evaluation_cache"] = {"semantic": semantic, "candidate": c, "samples": rows,
                                         "result": result, "frame_id": fingerprint}
        # Reuse the proven same-source shape at its original clock. Only the
        # cheap frozen-policy freshness check runs again on a cached stage.
        # Never mutate the cached result when this frame crosses its deadline.
        contract = self.execution_contracts.get(route)
        if reuse:
            freshness = self.core._owned(self.core._experiment_v2_freshness(
                c, result["baseline"], contract, at))
            result["confirmation_freshness"] = freshness
            if route == "C2" and freshness["value"] is not True:
                result["candidate_v2"] = self.core._experiment_result(
                    freshness["value"], freshness["reason"],
                    is_buy_point=False, production_permission=False)
            self.counts["v2_shape_reused"] += 1
        self.counts["v2_freshness_checked"] += 1
        if not identity_ok or invalid_order or source_conflict:
            reason = ("execution_binding_unproven" if not identity_ok else
                      "same_source_gate_or_quote_conflict" if source_conflict else "predicate_clock_reversed")
            for field in ("candidate_v2", "confirmation_freshness"):
                result[field] = self.core._experiment_result(
                    reason=reason, is_buy_point=False, production_permission=False)
        label_state = _hash([{key: result[arm].get(key) for key in ("status", "value", "reason")}
                             for arm in ("baseline", "candidate", "early_observation",
                                         "candidate_v2", "confirmation_freshness")])
        anchor = state["label_states"].get(label_state)
        selected = anchor is None and len(state["label_states"]) < MAX_LABEL_STATES
        if selected:
            state["label_states"][label_state] = fingerprint
            anchor = fingerprint
        sampling = {"policy": LABEL_SAMPLING_POLICY, "state_sha256": label_state,
                    "selected": selected, "anchor_frame_id": anchor,
                    "reason": "first_state" if selected else "repeated_state" if anchor else "state_capacity"}
        record = {"kind": "frame", "trade_date": c["trade_date"], "route": route,
                  "frame_id": fingerprint, "candidate_id": key, "predicate_asof": at.isoformat(),
                  "capture_input": p, "candidate_input": c, "sample_inputs": rows,
                  "execution_contract": self.core._owned(contract),
                  "input_sha256": _hash({"candidate": c, "samples": rows}),
                  "execution_strategy_version": binding.get("strategy_version"),
                  "execution_binding_valid": identity_ok,
                  "effective_account_id": effective_account_id,
                  "account_binding_basis": "startup_readonly_binding" if p.get("account_id") is None and route != "C3" else "producer",
                  "episode_id": episode, "label_sampling": sampling, "evaluation": evaluation, "result": result}
        self.emit(record)
        self.counts["evaluated_frames"] += 1
        self.watermarks["evaluated"] = at.isoformat()
        self.streams.move_to_end(key)
        while len(self.streams) > MAX_STREAMS:
            self.streams.popitem(last=False)
            self.counts["stream_evicted"] += 1
            self.emit({"kind": "gap", "trade_date": c["trade_date"], "reason": "stream_capacity",
                       "missing_count": None})
        if not selected:
            self.counts["label_repeated_state" if anchor else "label_state_capacity"] += 1
            if anchor is None:
                self.emit({"kind": "label_censored", "trade_date": c["trade_date"], "route": route,
                           "frame_id": fingerprint, "reason": "label_state_capacity",
                           "remaining_horizons": ["5", "15", "30"]})
            return
        if self.valid_quote(visible_quote, at):
            self.pending[fingerprint] = {"id": fingerprint, "route": route, "day": c["trade_date"],
                "code": c["code"], "anchor": at, "price": p["quote"]["price"],
                "basis": p["quote"]["prev_close"], "last": at, "last_observed": at, "gap": False,
                "targets": {str(m): self.core._horizon(at, m) for m in (5, 15, 30)},
                "last_quote": None, "mae": 0.0, "mfe": 0.0}
            while len(self.pending) > MAX_PENDING:
                _, old = self.pending.popitem(last=False)
                self.emit({"kind": "label_censored", "trade_date": old["day"], "route": old["route"],
                           "frame_id": old["id"], "reason": "pending_capacity",
                           "remaining_horizons": list(old["targets"])})
        else:
            self.emit({"kind": "label_censored", "trade_date": c["trade_date"], "route": route,
                       "frame_id": fingerprint, "reason": "invalid_reference_quote",
                       "remaining_horizons": ["5", "15", "30"]})

    def labels(self, quotes, at):
        # These accumulators never enter candidate/sample inputs or sealed predicates.
        for key, item in list(self.pending.items()):
            q = quotes.get(item["code"])
            source = _clock(q.get("source_quote_at")) if q else None
            if source and source > item["anchor"] and source.date().isoformat() == item["day"]:
                if q["prev_close"] != item["basis"] or source <= item["last"]:
                    item["gap"] = True
                elif (self.core._seconds(source) - self.core._seconds(item["last"]) > 75
                      or (_clock(q["observed_at"]) - item["last_observed"]).total_seconds() > 75):
                    item["gap"] = True
                if self.core._session(source) != self.core._session(item["anchor"]):
                    item["gap"] = True  # strict same-session labels; lunch is not continuity
            for horizon, target in list(item["targets"].items()):
                if target is None or at.date().isoformat() != item["day"] or at >= target:
                    endpoint = (q if source and target and source <= target
                                and _clock(q["observed_at"]) <= target else item["last_quote"])
                    end_source = _clock(endpoint["source_quote_at"]) if endpoint else None
                    valid = (target is not None and at.date().isoformat() == item["day"]
                             and not item["gap"] and end_source is not None
                             and end_source > item["anchor"]
                             and 0 <= (target-end_source).total_seconds() <= 30)
                    self.emit({"kind": "future_label", "trade_date": item["day"],
                               "route": item["route"], "frame_id": key, "horizon_minutes": int(horizon),
                               "outcome_as_of": at.isoformat(), "target_at": target.isoformat() if target else None,
                               "status": "observed" if valid else "unknown",
                               "reason": "sampled_reference_only" if valid else "gap_endpoint_or_right_censored",
                               "reference_markout_pct": round((endpoint["price"]/item["price"]-1)*100, 6) if valid else None,
                               "reference_is_fill": False, "net_profit": None,
                               "endpoint_quote": endpoint, "anchor_at": item["anchor"].isoformat()})
                    del item["targets"][horizon]
            if source and source > item["last"] and source.date().isoformat() == item["day"]:
                item["last"] = source
                item["last_observed"] = _clock(q["observed_at"])
                item["last_quote"] = q
            if not item["targets"]:
                del self.pending[key]


def read_candidate_shadow_report(*, trade_date, route=None, limit=200, output_dir=None):
    """Synchronous read-only bounded reader; API callers should use to_thread."""
    day = trade_date.isoformat() if type(trade_date) is date else str(trade_date)
    if date.fromisoformat(day).isoformat() != day:
        raise ValueError("ISO trade_date required")
    limit = max(1, min(int(limit), 1000))
    directory = Path(output_dir or DEFAULT_OUTPUT) / day
    started = time.monotonic()
    deadline = started + MAX_REPORT_SECONDS
    errors, files, frame_heap, record_heap, receipt_heap = [], [], [], [], []
    coverage_counts = Counter()
    reasons, read_files = set(), set()
    truncated, file_count, sequence, bytes_read, file_reads = False, 0, 0, 0, 0
    decoded_bytes = 0
    directory_complete = True
    frame_selection_complete = label_scan_complete = True

    class BudgetExhausted(Exception):
        pass

    def check_deadline():
        if time.monotonic() >= deadline:
            raise BudgetExhausted("time_budget")

    try:
        directory_fd = _open_shadow_directory(directory)
    except FileNotFoundError:
        directory_fd = None
    except OSError as exc:
        directory_fd = None
        directory_complete = False
        reasons.add("directory_validation_error")
        errors.append({"file": day, "error": type(exc).__name__})
    if directory_fd is not None:
        try:
            with os.scandir(directory_fd) as entries:
                for index, entry in enumerate(entries):
                    check_deadline()
                    if index >= MAX_REPORT_DIRECTORY_ENTRIES:
                        raise BudgetExhausted("directory_entry_budget")
                    if (entry.name.startswith("paper-research-")
                            and entry.name.endswith((".json", ".json.gz"))):
                        file_count += 1
                        if len(files) < MAX_REPORT_FILES:
                            heapq.heappush(files, entry.name)
                        elif entry.name > files[0]:
                            heapq.heapreplace(files, entry.name)
        except BudgetExhausted as exc:
            reasons.add(str(exc))
            directory_complete = False
        except OSError as exc:
            directory_complete = False
            reasons.add("directory_validation_error")
            errors.append({"file": day, "error": type(exc).__name__})
        finally:
            os.close(directory_fd)
    if file_count > MAX_REPORT_FILES:
        reasons.add("day_file_limit")
    truncated = bool(reasons)
    files.sort(reverse=True)  # Spend bounded bytes on newest publications first.

    def verified(name, *, first_pass=False):
        nonlocal bytes_read, decoded_bytes, file_reads
        check_deadline()
        compressed = name.endswith(".json.gz")
        suffix = ".json.gz" if compressed else ".json"
        # Both passes charge physical AND decoded work, including invalid files.
        cap = MAX_REPORT_TOTAL_BYTES // 2 if first_pass else MAX_REPORT_TOTAL_BYTES
        decoded_cap = MAX_REPORT_DECODED_BYTES // 2 if first_pass else MAX_REPORT_DECODED_BYTES
        directory_fd = _open_shadow_directory(directory)
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
        finally:
            os.close(directory_fd)
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            size = before.st_size
            if not stat.S_ISREG(before.st_mode) or size > MAX_REPORT_FILE_BYTES:
                raise ValueError("file_size_or_type")
            if bytes_read + size > cap:
                raise BudgetExhausted("frame_pass_byte_budget" if first_pass else "total_read_byte_budget")
            file_reads += 1
            read_files.add(name)
            chunks, consumed = [], 0
            digest = hashlib.sha256()
            while consumed < size:
                check_deadline()
                chunk = stream.read(min(64 * 1024, size - consumed))
                if not chunk:
                    raise ValueError("truncated_file")
                consumed += len(chunk)
                bytes_read += len(chunk)
                digest.update(chunk)
                chunks.append(chunk)
            after = os.fstat(stream.fileno())
            if ((before.st_size, before.st_mtime_ns, before.st_ctime_ns) !=
                    (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
                raise ValueError("file_changed")
        check_deadline()
        if not name.endswith("-" + digest.hexdigest()[:16] + suffix):
            raise ValueError("hash_mismatch")
        output, file_decoded = [], 0
        decoder = zlib.decompressobj(16 + zlib.MAX_WBITS) if compressed else None
        for chunk in chunks:
            pending = chunk
            while pending:
                check_deadline()
                allowance = min(MAX_REPORT_FILE_BYTES - file_decoded,
                                decoded_cap - decoded_bytes)
                if allowance <= 0:
                    raise BudgetExhausted("decoded_byte_budget")
                if decoder is None:
                    if len(pending) > allowance:
                        raise BudgetExhausted("decoded_byte_budget")
                    part, pending = pending, b""
                else:
                    # Precharge the output bound: decoder errors do not expose
                    # how much output was produced before CRC/format rejection.
                    charge = min(64 * 1024, allowance)
                    file_decoded += charge
                    decoded_bytes += charge
                    part = decoder.decompress(pending, charge)
                    # Successful calls refund only the unused reservation, before
                    # any trailing/member rejection can discard returned output.
                    file_decoded -= charge - len(part)
                    decoded_bytes -= charge - len(part)
                    pending = decoder.unconsumed_tail
                    # One gzip member only: reject concatenation, padding, junk.
                    if decoder.unused_data:
                        raise ValueError("gzip_trailing_data")
                output.append(part)
                if decoder is None:
                    file_decoded += len(part)
                    decoded_bytes += len(part)
        if decoder is not None and not decoder.eof:
            raise ValueError("gzip_truncated")
        check_deadline()
        payload = json.loads(b"".join(output))
        check_deadline()
        if type(payload) is not dict or type(payload.get("records")) is not list:
            raise ValueError("invalid_record_envelope")
        records = payload["records"]
        if any(type(record) is not dict for record in records):
            raise ValueError("invalid_record_type")
        return records

    def retain(heap, key, record, cap=None):
        nonlocal truncated, sequence
        sequence += 1
        item = (key, sequence, record)
        if len(heap) < (limit if cap is None else cap):
            heapq.heappush(heap, item)
        else:
            truncated = True
            if item[:2] > heap[0][:2]:
                heapq.heapreplace(heap, item)

    for name in files:
        try:
            check_deadline()
            if len(frame_heap) >= limit:
                published = datetime.strptime(name.split("-")[2], "%Y%m%dT%H%M%S%f")
                if published.isoformat() < frame_heap[0][0][0]:
                    reasons.add("row_limit")
                    break  # Older publication cannot contain a later captured predicate.
            for record in verified(name, first_pass=True):
                check_deadline()
                if record.get("trade_date") != day or (route is not None and record.get("route") not in (route, None)):
                    continue
                retain(record_heap, (record.get("recorded_at", ""), record.get("session_id", ""),
                                     record.get("frame_id", ""), record.get("kind", "")), record)
                if record.get("kind") == "frame":
                    if _stock_code(record.get("capture_input", {}).get("code")):
                        coverage_counts["stock_frame_records"] += 1
                        original = record["capture_input"].get("original_candidate")
                        coverage_counts["original_candidate_frames" if original is True else
                                        "explicit_noncandidate_frames" if original is False else
                                        "unknown_candidate_identity_frames"] += 1
                        retain(frame_heap, (record["predicate_asof"], record["session_id"], record["frame_id"]), record)
                    else:
                        coverage_counts["invalid_identity_records"] += 1
                elif record.get("kind") == "scan_receipt":
                    coverage_counts["non_stock_receipt_records"] += 1
                    receipt_type = record.get("receipt_type", "scan_issue")
                    if receipt_type in ("scan_completed", "not_scanned", "scan_issue"):
                        coverage_counts[receipt_type] += 1
                    minimal = {key: record.get(key) for key in (
                        "route", "stage", "reason", "scan_id", "receipt_type", "coverage_counts",
                        "predicate_asof", "session_id", "execution_binding_valid")}
                    counts = record.get("coverage_counts") or {}
                    minimal["counts"] = dict(counts)  # Lightweight UI contract alias.
                    known_scans = [counts[key] for key in ("visited_quote_count", "scanned_quote_count",
                                   "scanned_count", "visited_count", "total_scanned_count")
                                   if type(counts.get(key)) is int and counts[key] >= 0]
                    expected = known_scans[0] if known_scans and len(set(known_scans)) == 1 else None
                    raw_inputs = record.get("capture_input", {}).get("gate_inputs", {})
                    minimal["gate_inputs"] = {key: value for key, value in raw_inputs.items()
                                              if key.endswith("_count") and type(value) in (int, float)}
                    minimal["gate_inputs"]["counts"] = counts
                    minimal["expected_scanned_count"] = expected
                    minimal["expected_scanned_status"] = (
                        "not_scanned" if receipt_type == "not_scanned" else
                        "producer_reported" if expected is not None else "unknown")
                    minimal["expected_candidate_count"] = (
                        counts.get("candidate_count") if type(counts.get("candidate_count")) is int else None)
                    minimal["diagnostic_count"] = (
                        counts.get("diagnostic_count") if type(counts.get("diagnostic_count")) is int else None)
                    retain(receipt_heap, (record.get("predicate_asof") or "", record.get("session_id") or "",
                                          record.get("scan_id") or "", record.get("stage") or ""), minimal,
                           cap=min(limit, 200))
                elif record.get("kind") == "invalid_identity":
                    coverage_counts["invalid_identity_records"] += 1
        except BudgetExhausted as exc:
            reasons.add(str(exc))
            frame_selection_complete = False
            break
        except Exception as exc:
            frame_selection_complete = False
            if len(errors) < 20:
                errors.append({"file": name, "error": type(exc).__name__})
    rows, anchors = [], {}
    for _, _, record in sorted(frame_heap, reverse=True):
        packet, result = record["capture_input"], record["result"]
        sampling = record.get("label_sampling") or {"policy": "legacy_every_frame",
                                                   "selected": True, "anchor_frame_id": record["frame_id"]}
        row = {
            "frame_id": record["frame_id"], "candidate_id": record["candidate_id"],
            "episode_id": record.get("episode_id"), "route": record["route"],
            "code": packet.get("code"), "name": packet.get("name"),
            "stage": packet.get("stage"), "reason": packet.get("reason"),
            "scan_id": packet.get("scan_id"), "predicate_asof": record["predicate_asof"],
            "source_quote_at": packet.get("quote", {}).get("source_quote_at"),
            "observed_at": record["predicate_asof"],
            "recorded_at": record.get("recorded_at"),
            "reference_price": packet.get("quote", {}).get("price"),
            "baseline": result["baseline"], "candidate": result["candidate"],
            "candidate_v2": result.get("candidate_v2") or {
                "status": "unknown", "value": None, "reason": "legacy_record_v2_unavailable",
                "first_event": None, "is_buy_point": False, "production_permission": False},
            "confirmation_freshness": result.get("confirmation_freshness") or {
                "status": "unknown", "value": None, "reason": "legacy_record_freshness_unavailable",
                "first_event": None, "is_buy_point": False, "production_permission": False},
            "research_v2_version": result.get("research_v2_version"),
            "early_observation": result["early_observation"],
            "producer_scan_guard": result.get("producer_scan_guard"),
            "sealed_candidate": result.get("sealed_candidate"),
            "production_version": result.get("production_version"),
            "execution_strategy_version": record.get("execution_strategy_version"),
            "account_id": record.get("effective_account_id", result.get("account_id")),
            "account_name": result.get("account_name"),
            "execution_binding_valid": record.get("execution_binding_valid"),
            "input_sha256": record["input_sha256"], "future_labels": [],
            "label_sampling": sampling, "evaluation": record.get("evaluation"),
            "session_id": record["session_id"],
            "production_permission": False, "reference_is_fill": False, "net_profit": None,
        }
        rows.append(row)
        anchor = sampling.get("anchor_frame_id") or record["frame_id"]
        anchors.setdefault((record["session_id"], anchor), []).append(row)
    # Second bounded pass joins all later outcome records, even when anchors are
    # outside latest-N rows. Never let raw labels consume the frame row limit.
    for name in files if anchors else ():
        try:
            for record in verified(name):
                check_deadline()
                if record.get("kind") not in ("future_label", "label_censored"):
                    continue
                for row in anchors.get((record.get("session_id"), record.get("frame_id")), ()):
                    if len(row["future_labels"]) < 4:
                        row["future_labels"].append(record)
                    else:
                        truncated = True
        except BudgetExhausted as exc:
            reasons.add(str(exc))
            label_scan_complete = False
            break
        except Exception as exc:
            label_scan_complete = False
            if len(errors) < 20:
                errors.append({"file": name, "error": type(exc).__name__})
    if not directory_complete or file_count > MAX_REPORT_FILES:
        frame_selection_complete = label_scan_complete = False
    if not frame_selection_complete and not anchors:
        label_scan_complete = False
    if errors:
        reasons.add("file_validation_errors")
    if truncated:
        reasons.add("row_or_record_limit")
    partial = bool(reasons) or truncated
    records = [item[2] for item in sorted(record_heap, reverse=True)]
    recent_receipts = [item[2] for item in sorted(receipt_heap, reverse=True)]
    route_scans = {}
    for receipt in recent_receipts:
        route_key = receipt.get("route") or "unbound"
        identity = (receipt.get("session_id"), receipt.get("scan_id"))
        if route_key not in route_scans:
            route_scans[route_key] = {
                "session_id": identity[0], "scan_id": identity[1],
                "expected_scanned_count": None, "expected_scanned_status": "unknown",
                "expected_candidate_count": None, "diagnostic_count": None,
                "latest_stage": receipt.get("stage"), "latest_reason": receipt.get("reason")}
        scan = route_scans[route_key]
        if (scan["session_id"], scan["scan_id"]) != identity:
            continue
        for field in ("expected_scanned_count", "expected_candidate_count", "diagnostic_count"):
            if scan[field] is None and receipt.get(field) is not None:
                scan[field] = receipt[field]
        if receipt.get("expected_scanned_status") in ("producer_reported", "not_scanned"):
            scan["expected_scanned_status"] = receipt["expected_scanned_status"]
    coverage_status = "partial" if partial else "observed" if rows or recent_receipts else "unknown"
    coverage_reason = ("bounded_read_partial:" + ",".join(sorted(reasons)) if partial else
                       "delivered_projection_only;full_day_denominator_unknown" if rows or recent_receipts else
                       "no_matching_evidence_received;full_day_denominator_unknown")
    return {"schema": "candidate_shadow_readonly_v1", "trade_date": day, "route": route,
            "status": candidate_shadow_status(), "status_scope": "current_process_not_historical_day",
            "records": records, "rows": rows, "row_count": len(rows),
            "summary": {"scope": "delivered_original_candidate_projection",
                        "read_scope": "bounded_read_subset", "full_day_denominator": False,
                        "frame_records": coverage_counts["stock_frame_records"],
                        "original_candidate_frame_records": coverage_counts["original_candidate_frames"],
                        "explicit_noncandidate_frame_records": coverage_counts["explicit_noncandidate_frames"],
                        "unknown_candidate_identity_frame_records": coverage_counts["unknown_candidate_identity_frames"],
                        "expected_scanned_count": None, "expected_scanned_status": "per_route_receipt_or_unknown"},
            "coverage": {"scope": "bounded_read_subset", "full_day_denominator": False,
                         "status": coverage_status, "reason": coverage_reason,
                         "projection_scope": "delivered_original_candidate_projection",
                         "expected_scanned_count": None,
                         "expected_scanned_status": "per_route_receipt_or_unknown",
                         "expected_scanned_by_route": route_scans,
                         "stock_frame_records": coverage_counts["stock_frame_records"],
                         "non_stock_receipt_records": coverage_counts["non_stock_receipt_records"],
                         "invalid_identity_records": coverage_counts["invalid_identity_records"],
                         "receipt_types": {key: coverage_counts[key] for key in
                                           ("scan_completed", "not_scanned", "scan_issue")},
                         "recent_receipts": recent_receipts, "latest_receipts": recent_receipts,
                         "recent_receipt_limit": min(limit, 200)},
            "count": len(records), "limit": limit, "truncated": partial,
            "partial": partial, "partial_reasons": sorted(reasons),
            "frame_selection_complete": frame_selection_complete,
            "labels_complete": label_scan_complete, "label_scan_complete": label_scan_complete,
            "directory_complete": directory_complete, "full_day_denominator": False,
            "bytes_read": bytes_read, "byte_budget": MAX_REPORT_TOTAL_BYTES,
            "decoded_bytes": decoded_bytes, "decoded_byte_budget": MAX_REPORT_DECODED_BYTES,
            "file_byte_budget": MAX_REPORT_FILE_BYTES,
            "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
            "time_budget_seconds": MAX_REPORT_SECONDS, "file_reads": file_reads,
            "order": "predicate_asof_desc_session_frame_id", "label_sampling_policy": LABEL_SAMPLING_POLICY,
            "scanned_files": len(read_files), "selected_files": len(files),
            "total_files": file_count, "scan_limit": MAX_REPORT_FILES,
            "errors": errors, "read_only": True, "production_permission": False,
            "reference_is_fill": False, "net_profit": None}
