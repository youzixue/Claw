"""Exercise the standalone no-network gate without any application/network service."""
import io
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from scripts.pytest_no_network import NetworkAudit, BLOCKED_EVENTS, _exclusive_json_file


@pytest.mark.parametrize("event", sorted(BLOCKED_EVENTS))
def test_callback_counts_even_swallowed_attempts_without_serializing_args(monkeypatch, event):
    class NeverRead:
        def __repr__(self):
            raise AssertionError("must not inspect live event args")
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "test_fixture.py::case (call)")
    stream = io.StringIO()
    audit = NetworkAudit(event_stream=stream)
    with pytest.raises(AssertionError, match="denied"):
        audit(event, (NeverRead(),))
    result = audit.report(0, completed=True)
    assert result["passed"] is False and result["network_attempt_count"] == 1
    assert result["node_counts"] == {"test_fixture.py::case (call)": 1}
    assert json.loads(stream.getvalue())["event"] == event
    assert result["records"][0]["stack"]


def test_record_cap_does_not_silently_drop_total_or_gate_failure():
    audit = NetworkAudit(max_records=1)
    for _ in range(3):
        with pytest.raises(AssertionError):
            audit("socket.connect", ())
    result = audit.report(0, completed=True)
    assert result["network_attempt_count"] == 3 and result["records_dropped"] == 2
    assert len(result["records"]) == 1 and not result["passed"]


def test_unrelated_events_are_allowed_and_incomplete_pytest_is_not_pass():
    audit = NetworkAudit()
    audit("sqlite3.connect", ())
    assert audit.report(0, completed=True)["passed"] is True
    assert audit.report(None, completed=False)["passed"] is False
    assert audit.report(1, completed=True)["passed"] is False


def test_output_cannot_replace_existing_file_or_database_suffix(tmp_path):
    path = tmp_path / "report.json"
    path.write_text("prior evidence")
    with pytest.raises(FileExistsError):
        _exclusive_json_file(path, ".json")
    with pytest.raises(ValueError):
        _exclusive_json_file(tmp_path / "claw.db", ".json")
    assert path.read_text() == "prior evidence"


@pytest.mark.parametrize("assertion,expected", [("True", 0), ("False", 1)])
def test_real_cli_keeps_pytest_failure_separate_from_network(tmp_path, assertion, expected):
    case = tmp_path / "test_plain.py"
    case.write_text("def test_plain():\n    assert " + assertion + "\n")
    script = Path(__file__).resolve().parents[1] / "scripts/pytest_no_network.py"
    report, events = tmp_path / "report.json", tmp_path / "events.jsonl"
    result = subprocess.run([sys.executable, "-B", str(script), "--report", str(report),
        "--events", str(events), "--", str(case), "-q", "-p", "no:cacheprovider"],
        cwd=tmp_path, env={**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
        capture_output=True, text=True, timeout=20)
    assert result.returncode == expected
    payload = json.loads(report.read_text())
    assert payload["network_attempt_count"] == 0
    assert payload["pytest_completed"] is True and payload["pytest_returncode"] == expected
    assert payload["passed"] is (expected == 0)
    assert events.read_text() == ""


def test_real_cli_detects_caught_dns_with_correct_node_and_does_not_retry(tmp_path):
    case = tmp_path / "test_attempt.py"
    case.write_text("""
import socket
def test_attempt():
    try:
        socket.getaddrinfo('must-not-resolve.invalid', 443)
    except AssertionError:
        pass
""")
    backend = Path(__file__).resolve().parents[1]
    script = backend / "scripts/pytest_no_network.py"
    report, events = tmp_path / "report.json", tmp_path / "events.jsonl"
    cmd = [sys.executable, "-B", str(script), "--report", str(report), "--events", str(events),
           "--", str(case), "-q", "-p", "no:cacheprovider"]
    env = {**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
    result = subprocess.run(cmd, cwd=tmp_path, env=env, capture_output=True, text=True, timeout=20)
    assert result.returncode == 1 and "1 passed" in result.stdout
    payload = json.loads(report.read_text())
    assert payload["pytest_returncode"] == 0 and payload["network_attempt_count"] == 1
    assert list(payload["node_counts"])[0].endswith("test_attempt.py::test_attempt (call)")
    before = report.read_bytes()
    retry = subprocess.run(cmd, cwd=tmp_path, env=env, capture_output=True, text=True, timeout=20)
    assert retry.returncode != 0 and "passed" not in retry.stdout
    assert report.read_bytes() == before
