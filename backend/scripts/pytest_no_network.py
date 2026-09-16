"""Fail pytest when its main process attempts outbound network, even if caught.

Only an explicit CLI invocation installs the process-lifetime Python audit hook.
This is not a security sandbox and does not propagate hooks into child processes.
No application/settings/database imports occur here; existing conftest owns DB isolation.
"""
import argparse
from collections import Counter
from contextlib import ExitStack
import json
import os
from pathlib import Path
import sys
import threading
import traceback


BLOCKED_EVENTS = frozenset({
    "socket.connect", "socket.sendto", "socket.getaddrinfo",
    "socket.gethostbyname", "socket.gethostbyaddr", "socket.getnameinfo",
})


class NetworkAudit:
    def __init__(self, *, event_stream=None, max_records=1000):
        if type(max_records) is not int or max_records < 1:
            raise ValueError("max_records must be a positive integer")
        self.event_stream = event_stream
        self.max_records = max_records
        self.records = []
        self.event_counts = Counter()
        self.node_counts = Counter()
        self.lock = threading.Lock()

    def __call__(self, event, args):
        if event not in BLOCKED_EVENTS:
            return
        node = os.environ.get("PYTEST_CURRENT_TEST") or "<collection-or-outside-test>"
        # Read only diagnostic leaves. Never store socket objects, addresses,
        # HTTP payloads, proxy URLs, environment contents or source text.
        frames = [{"file": frame.filename, "line": frame.lineno, "function": frame.name}
                  for frame in traceback.extract_stack(limit=64)]
        record = {"event": event, "pytest_current_test": node, "stack": frames}
        with self.lock:
            self.event_counts[event] += 1
            self.node_counts[node] += 1
            if len(self.records) < self.max_records:
                self.records.append(record)
                if self.event_stream is not None:
                    self.event_stream.write(json.dumps(record, ensure_ascii=False) + "\n")
                    self.event_stream.flush()
        raise AssertionError("pytest network audit denied " + event)

    def report(self, pytest_returncode, *, completed):
        with self.lock:
            count = sum(self.event_counts.values())
            return {
                "protocol": "claw_pytest_no_network_v1",
                "scope": "main pytest process and threads; child processes not covered",
                "pytest_completed": completed, "pytest_returncode": pytest_returncode,
                "network_attempt_count": count, "event_counts": dict(self.event_counts),
                "node_counts": dict(self.node_counts), "records": list(self.records),
                "records_dropped": count - len(self.records),
                "passed": completed and pytest_returncode == 0 and count == 0,
            }


def _exclusive_json_file(path, suffix):
    if not path.is_absolute() or path.suffix != suffix:
        raise ValueError("diagnostic paths must be absolute and end in " + suffix)
    path = path.parent.resolve(strict=True) / path.name
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    return os.fdopen(descriptor, "w", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--events", type=Path, required=True)
    parser.add_argument("pytest_args", nargs=argparse.REMAINDER)
    options = parser.parse_args()
    args = options.pytest_args
    if args[:1] == ["--"]:
        args = args[1:]
    if not args:
        parser.error("provide explicit pytest test selection after --")
    # Refuse preexisting output before running tests. Never replace evidence.
    with ExitStack() as stack:
        report_file = stack.enter_context(_exclusive_json_file(options.report, ".json"))
        event_file = stack.enter_context(_exclusive_json_file(options.events, ".jsonl"))
        audit = NetworkAudit(event_stream=event_file)
        sys.addaudithook(audit)
        backend = str(Path(__file__).resolve().parents[1])
        if backend not in sys.path:
            sys.path.insert(0, backend)
        code, completed = None, False
        try:
            import pytest
            code = int(pytest.main(args))
            completed = True
        finally:
            result = audit.report(code, completed=completed)
            json.dump(result, report_file, ensure_ascii=False, indent=2, allow_nan=False)
            report_file.write("\n")
            report_file.flush()
        print("network_attempt_count:", result["network_attempt_count"], flush=True)
        print("no_network_gate_passed:", result["passed"], flush=True)
        return code or (1 if result["network_attempt_count"] else 0)


if __name__ == "__main__":
    raise SystemExit(main())
