"""Observer storage only: synthetic temp files, no core execution, DB or network."""
from datetime import datetime
import gzip
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from app.paper import candidate_shadow as s

AT = datetime(2026, 9, 24, 10)
DAY = "2026-09-24"


@pytest.fixture
def root(tmp_path):
    return tmp_path.resolve()


def payload(kind="gap", **extra):
    return {"schema": "candidate_shadow_runtime_v1", "core_sha256": "storage-only",
            "session_id": "session", "production_permission": False,
            "records": [{"kind": kind, "trade_date": DAY, "session_id": "session",
                         "recorded_at": AT.isoformat(), "reason": "失败/unknown",
                         "original_confirmed_at": None, "value": False, **extra}]}


def put(root, content, suffix=".json.gz", at=AT):
    directory = root / DAY
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (f"paper-research-{at:%Y%m%dT%H%M%S%f}-"
                        + hashlib.sha256(content).hexdigest()[:16] + suffix)
    path.write_bytes(content)
    return path


def read(root, **kwargs):
    return s.read_candidate_shadow_report(trade_date=DAY, output_dir=root, **kwargs)


def old_bytes(data):
    return (json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2,
                       allow_nan=False) + "\n").encode()


def test_old_new_complete_equality_and_determinism(root):
    data = payload(samples=[{"clock": AT.isoformat(), "unknown": None, "failed": False,
                             "number": 1.234567890123, "unicode": "中文"}] * 40)
    encoded = s._encode_shadow(data)
    assert encoded == s._encode_shadow(data)
    assert json.loads(gzip.decompress(encoded)) == json.loads(old_bytes(data)) == data
    assert len(encoded) < len(old_bytes(data))
    old = put(root, old_bytes(data), ".json")
    legacy = read(root)
    old.unlink()  # Own synthetic temp fixture only.
    new = Path(s._publish_shadow(encoded, root / DAY, AT)["output"])
    modern = read(root)
    assert modern["records"] == legacy["records"] == data["records"]
    assert not modern["partial"] and not legacy["partial"]
    assert modern["bytes_read"] == new.stat().st_size
    assert modern["decoded_bytes"] == len(gzip.decompress(encoded))


def test_mixed_formats_join_labels(root):
    frame = payload("frame", route="B2", frame_id="f", candidate_id="c",
                    predicate_asof=AT.isoformat(), capture_input={"code": "600001"},
                    input_sha256="owned", result={arm: {"status": "unknown", "value": None}
                    for arm in ("baseline", "candidate", "early_observation")})
    label = payload("future_label", frame_id="f", horizon_minutes=5, status="unknown")
    put(root, old_bytes(frame), ".json")
    put(root, s._encode_shadow(label), at=AT.replace(minute=5))
    result = read(root)
    assert result["total_files"] == 2 and result["file_reads"] == 4
    assert result["rows"][0]["future_labels"] == label["records"]
    assert result["labels_complete"] and not result["partial"]


@pytest.mark.parametrize("failure", ["link", "fsync"])
def test_atomic_failure_no_publication_or_temp(root, monkeypatch, failure):
    def fail(*args, **kwargs):
        raise OSError("injected")
    monkeypatch.setattr(s.os, failure, fail)
    with pytest.raises(OSError):
        s._publish_shadow(s._encode_shadow(payload()), root / DAY, AT)
    assert list((root / DAY).iterdir()) == []


def test_collision_never_overwrites(root):
    content = s._encode_shadow(payload())
    path = Path(s._publish_shadow(content, root / DAY, AT)["output"])
    path.write_bytes(b"existing-evidence")
    with pytest.raises(FileExistsError):
        s._publish_shadow(content, root / DAY, AT)
    assert path.read_bytes() == b"existing-evidence"
    assert list((root / DAY).iterdir()) == [path]


@pytest.mark.parametrize("change", ["truncate", "crc", "trailing", "concat", "invalid_json", "envelope", "record"])
def test_gzip_corruption_fail_closed(root, change):
    content = s._encode_shadow(payload())
    if change == "truncate":
        content = content[:-1]
    elif change == "crc":
        content = content[:-8] + bytes([content[-8] ^ 1]) + content[-7:]
    elif change == "trailing":
        content += b"x"
    elif change == "concat":
        content += content
    elif change == "invalid_json":
        content = gzip.compress(b"{")
    elif change == "envelope":
        content = gzip.compress(b'{"records":{}}')
    elif change == "record":
        content = gzip.compress(b'{"records":[false]}')
    put(root, content)  # Correct compressed hash: decoder itself must reject.
    result = read(root)
    assert result["errors"] and result["records"] == [] and result["partial"]
    assert not result["frame_selection_complete"] and not result["labels_complete"]


@pytest.mark.parametrize("corruption", ["trailing", "concat", "crc"])
def test_many_bad_gzip_files_charge_all_decoder_work(root, monkeypatch, corruption):
    # Exact independent-review reproduction, expanded to concatenation and CRC.
    raw = json.dumps({"records": [{"kind": "gap", "trade_date": DAY,
                                  "padding": "x" * 3500}]}).encode()
    assert len(raw) == 3573
    encoded = gzip.compress(raw, mtime=0)
    if corruption == "trailing":
        encoded += b"x"
    elif corruption == "concat":
        encoded += encoded
    else:
        encoded = encoded[:-8] + bytes([encoded[-8] ^ 1]) + encoded[-7:]
    for second in range(20):
        put(root, encoded, at=AT.replace(second=second))
    monkeypatch.setattr(s, "MAX_REPORT_DECODED_BYTES", 8192)
    factory = s.zlib.decompressobj
    measured = {"returned": 0, "calls": 0, "unreturned_bounds": 0}
    class Meter:
        def __init__(self, *args, **kwargs):
            self.decoder = factory(*args, **kwargs)
        def decompress(self, content, max_length):
            measured["calls"] += 1
            try:
                part = self.decoder.decompress(content, max_length)
            except s.zlib.error:
                measured["unreturned_bounds"] += max_length
                raise
            measured["returned"] += len(part)
            return part
        def __getattr__(self, name):
            return getattr(self.decoder, name)
    monkeypatch.setattr(s.zlib, "decompressobj", Meter)
    result = read(root)
    assert result["records"] == [] and result["partial"]
    assert "decoded_byte_budget" in result["partial_reasons"]
    assert result["decoded_bytes"] == 4096
    assert measured["returned"] + measured["unreturned_bounds"] == result["decoded_bytes"]
    assert measured["calls"] <= 2 and result["file_reads"] == 2
    if corruption == "crc":
        assert measured["unreturned_bounds"] == 4096
    else:
        assert measured["returned"] == 4096
    print("BAD_GZIP_BUDGET=" + json.dumps({"corruption": corruption, **measured,
          "reported_decoded_bytes": result["decoded_bytes"],
          "physical_bytes": result["bytes_read"], "file_reads": result["file_reads"],
          "partial_reasons": result["partial_reasons"]}, sort_keys=True))


@pytest.mark.parametrize("suffix", [".json", ".json.gz"])
def test_hash_mismatch(root, suffix):
    content = old_bytes(payload()) if suffix == ".json" else s._encode_shadow(payload())
    path = put(root, content, suffix)
    path.write_bytes(b"x" + content[1:])
    result = read(root)
    assert result["errors"] and not result["records"]


@pytest.mark.parametrize("where", ["file", "day", "parent"])
def test_symlinks_fail_closed(root, where):
    target = root / "real"
    content = s._encode_shadow(payload())
    path = put(target, content)
    if where == "file":
        directory = root / DAY
        directory.mkdir()
        (directory / path.name).symlink_to(path)
        source = root
    elif where == "day":
        (root / DAY).symlink_to(target / DAY, target_is_directory=True)
        source = root
    else:
        source = root / "linked"
        source.symlink_to(target, target_is_directory=True)
    result = read(source)
    assert result["errors"] and not result["records"] and result["partial"]


def test_writer_rejects_day_symlink(root):
    target = root / "real"
    target.mkdir()
    (root / DAY).symlink_to(target, target_is_directory=True)
    with pytest.raises(OSError):
        s._publish_shadow(s._encode_shadow(payload()), root / DAY, AT)
    assert not list(target.iterdir())


@pytest.mark.parametrize("delta", [-1, 0, 1])
def test_physical_budget_boundary(root, monkeypatch, delta):
    content = s._encode_shadow(payload())
    put(root, content)
    monkeypatch.setattr(s, "MAX_REPORT_TOTAL_BYTES", 2 * (len(content) + delta))
    result = read(root)
    assert bool(result["records"]) is (delta >= 0)
    assert result["bytes_read"] <= result["byte_budget"]
    if delta < 0:
        assert "frame_pass_byte_budget" in result["partial_reasons"]


@pytest.mark.parametrize("delta", [-1, 0, 1])
def test_decoded_budget_boundary(root, monkeypatch, delta):
    content = s._encode_shadow(payload())
    put(root, content)
    monkeypatch.setattr(s, "MAX_REPORT_DECODED_BYTES", 2 * (len(gzip.decompress(content)) + delta))
    result = read(root)
    assert bool(result["records"]) is (delta >= 0)
    assert result["decoded_bytes"] <= result["decoded_byte_budget"]


def test_bomb_stops_before_materializing_whole_json(root, monkeypatch):
    put(root, s._encode_shadow(payload(huge="x" * 2_000_000)))
    monkeypatch.setattr(s, "MAX_REPORT_DECODED_BYTES", 8192)
    result = read(root)
    assert not result["records"] and "decoded_byte_budget" in result["partial_reasons"]
    assert result["decoded_bytes"] <= 4096


def test_per_file_cap_and_zero_time(root, monkeypatch):
    content = s._encode_shadow(payload())
    put(root, content)
    monkeypatch.setattr(s, "MAX_REPORT_FILE_BYTES", len(content) - 1)
    result = read(root)
    assert result["errors"] and result["bytes_read"] == 0
    monkeypatch.setattr(s, "MAX_REPORT_SECONDS", 0)
    result = read(root)
    assert "time_budget" in result["partial_reasons"] and result["bytes_read"] == 0


def test_nonregular_file_does_not_block(root):
    directory = root / DAY
    directory.mkdir()
    os.mkfifo(directory / ("paper-research-20260924T100000000000-" + "a" * 16 + ".json"))
    result = read(root)
    assert result["errors"] and result["bytes_read"] == 0


@pytest.mark.parametrize("delta", [-1, 0, 1])
def test_reserve_uses_exact_encoded_size_plus_slack(root, monkeypatch, delta):
    content = s._encode_shadow(payload())
    expected = len(content) + s.STORAGE_ALLOCATION_SLACK_BYTES
    monkeypatch.setattr(s.os, "statvfs", lambda path: SimpleNamespace(
        f_bavail=s.MIN_DISK_RESERVE_BYTES + expected + delta, f_frsize=1))
    r = s._Runtime(root, {})
    assert r.disk_allows(root / DAY, content) is (delta >= 0)
    assert r.disk_budget["next_write_bytes"] == expected
    assert r.disk_budget["reserve_bytes"] == 5 * 1024 ** 3
    assert r.disk_budget["blocked"] is (delta < 0)


def test_flush_encodes_once_and_preserves_all_records(root, monkeypatch):
    monkeypatch.setattr(s.os, "statvfs", lambda p: SimpleNamespace(f_bavail=100 * 1024**3, f_frsize=1))
    original, calls = s.json.dumps, []
    def counted(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)
    monkeypatch.setattr(s.json, "dumps", counted)
    r = s._Runtime(root, {})
    r.core_sha = "storage-test-not-sealed-core"
    r.batch = payload()["records"] * 64
    r.flush()
    assert len(calls) == 1 and r.counts["durable_records"] == 64
    path = next((root / DAY).iterdir())
    assert json.loads(gzip.decompress(path.read_bytes()))["records"] == payload()["records"] * 64
    status = r.status()
    assert status["storage_format"] == "compact_json_gzip_level1_v1"
    assert status["storage_bytes"] == {"scope": "current_process", "encoded_total": path.stat().st_size,
                                      "published_total": path.stat().st_size,
                                      "last_encoded": path.stat().st_size}
    assert len(calls) == 1  # Status does not serialize the payload again.


@pytest.mark.parametrize("delta", [-1, 0, 1])
def test_decoded_per_file_boundary(root, monkeypatch, delta):
    content = s._encode_shadow(payload(large="z" * 5000))
    put(root, content)
    monkeypatch.setattr(s, "MAX_REPORT_FILE_BYTES", len(gzip.decompress(content)) + delta)
    result = read(root)
    assert bool(result["records"]) is (delta >= 0)
    assert result["decoded_bytes"] <= s.MAX_REPORT_FILE_BYTES


def test_label_pass_charges_both_budgets(root, monkeypatch):
    data = payload("frame", route="B2", frame_id="f", candidate_id="c",
                   predicate_asof=AT.isoformat(), capture_input={"code": "600001"},
                   input_sha256="owned", result={arm: {"status": "unknown", "value": None}
                   for arm in ("baseline", "candidate", "early_observation")})
    content = s._encode_shadow(data)
    put(root, content)
    monkeypatch.setattr(s, "MAX_REPORT_TOTAL_BYTES", len(content) * 2)
    monkeypatch.setattr(s, "MAX_REPORT_DECODED_BYTES", len(gzip.decompress(content)) * 2)
    result = read(root)
    assert result["bytes_read"] == result["byte_budget"]
    assert result["decoded_bytes"] == result["decoded_byte_budget"]
    assert result["file_reads"] == 2 and result["labels_complete"]


def test_deadline_after_parse_discards_whole_file(root, monkeypatch):
    put(root, s._encode_shadow(payload()))
    now = [0.0]
    monkeypatch.setattr(s.time, "monotonic", lambda: now[0])
    original = s.json.loads
    def delayed(*args, **kwargs):
        result = original(*args, **kwargs)
        now[0] = s.MAX_REPORT_SECONDS
        return result
    monkeypatch.setattr(s.json, "loads", delayed)
    result = read(root)
    assert "time_budget" in result["partial_reasons"] and not result["records"]


@pytest.mark.parametrize("mode", ["changed", "truncated"])
def test_regular_file_race_fails_closed(root, monkeypatch, mode):
    put(root, s._encode_shadow(payload()))
    original, calls = s.os.fstat, []
    def changed(fd):
        value = original(fd)
        calls.append(1)
        return SimpleNamespace(st_size=value.st_size + (1 if mode == "truncated" else 0),
                               st_mode=value.st_mode,
                               st_mtime_ns=value.st_mtime_ns + (1 if len(calls) > 1 else 0),
                               st_ctime_ns=value.st_ctime_ns)
    monkeypatch.setattr(s.os, "fstat", changed)
    result = read(root)
    assert result["errors"] and not result["records"]


def test_statvfs_failure_stops_without_writing(root, monkeypatch):
    monkeypatch.setattr(s.os, "statvfs", lambda p: (_ for _ in ()).throw(OSError("statvfs")))
    r = s._Runtime(root, {})
    r.core_sha = "storage-only"
    r.batch = payload()["records"]
    r.flush()
    assert r.disk_budget["reason"] == "disk_space_query_failed"
    assert r.counts["disk_budget_undurable_records"] == 1
    assert not list(root.iterdir()) and not r.active


def test_empty_day_contract(root):
    result = read(root)
    assert result["records"] == result["rows"] == [] and not result["errors"]
    assert result["coverage"]["status"] == "unknown" and not result["partial"]


def test_flush_failure_records_undurable_and_no_retry(root, monkeypatch):
    monkeypatch.setattr(s.os, "statvfs", lambda p: SimpleNamespace(f_bavail=100 * 1024**3, f_frsize=1))
    monkeypatch.setattr(s, "_publish_shadow", lambda *a: (_ for _ in ()).throw(OSError("disk")))
    r = s._Runtime(root, {})
    r.core_sha = "storage-test"
    r.batch = payload()["records"]
    r.flush()
    assert r.counts["undurable_records"] == 1 and r.counts["write_failed"] == 1
    assert r.counts["durable_records"] == 0 and r.batch == []
    sizes = r.status()["storage_bytes"]
    assert sizes["encoded_total"] == sizes["last_encoded"] > 0
    assert sizes["published_total"] == 0


def test_not_started_reports_storage_contract(monkeypatch):
    monkeypatch.setattr(s, "_runtime", None)
    status = s.candidate_shadow_status()
    assert status["storage_format"] == "compact_json_gzip_level1_v1"
    assert status["storage_bytes"] == {"scope": "current_process", "encoded_total": 0,
                                      "published_total": 0, "last_encoded": 0}
