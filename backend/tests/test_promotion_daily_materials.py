import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app.promotion.modeling.daily_materials import MaterialArchive, ReviewedSourcePolicy, MAX_BYTES, encode


def test_observed_now_archive_cannot_self_certify(tmp_path):
    archive = MaterialArchive(tmp_path)
    ref = archive.archive(b'{"final":true}', {"source": "ths", "source_version": "v1",
                                             "received_at": "2000-01-01T00:00:00"})
    result = archive.read(ref)
    assert result["status"] == "blocked"
    assert "source_protocol_unreviewed" in result["reasons"]
    assert result["manifest"]["received_at"] != "2000-01-01T00:00:00"
    assert result["manifest"]["first_available_at"] is None


def test_content_addressing_no_overwrite_and_size(tmp_path):
    archive = MaterialArchive(tmp_path)
    a = archive.put(b"first")
    assert a == archive.put(b"first")
    b = archive.put(b"second")
    assert a != b and archive.read_bytes(a) == b"first"
    with pytest.raises(ValueError):
        archive.put(b"")
    with pytest.raises(ValueError):
        archive.put(b"x" * (MAX_BYTES + 1))
    with pytest.raises(ValueError):
        archive.read_bytes({**a, "size": 1})


@pytest.mark.parametrize("path", ["../outside", "/etc/passwd", "objects/../outside", "objects/x.blob"])
def test_reference_path_escape_rejected(tmp_path, path):
    archive = MaterialArchive(tmp_path)
    ref = archive.put(b"one")
    with pytest.raises(ValueError):
        archive.read_bytes({**ref, "path": path})


def test_physical_sha_and_symlink_checks(tmp_path):
    archive = MaterialArchive(tmp_path / "archive")
    ref = archive.put(b"one")
    path = archive.root / ref["path"]
    path.write_bytes(b"two")
    with pytest.raises(ValueError, match="SHA"):
        archive.read_bytes(ref)
    path.unlink()
    outside = tmp_path / "outside"
    outside.write_bytes(b"one")
    path.symlink_to(outside)
    with pytest.raises(ValueError, match="symlink"):
        archive.read_bytes(ref)
    with pytest.raises(ValueError, match="symlink"):
        MaterialArchive(path / "child")


def test_atomic_publish_failure_does_not_publish_ready(tmp_path, monkeypatch):
    archive = MaterialArchive(tmp_path)
    import os
    monkeypatch.setattr(os, "link", lambda *args: (_ for _ in ()).throw(OSError("fixture failure")))
    with pytest.raises(OSError):
        archive.publish({"values": 1})
    assert not list(tmp_path.rglob("*.blob"))
    assert not list(tmp_path.rglob(".pending-*"))


def test_duplicate_json_keys_fail_closed(tmp_path):
    archive = MaterialArchive(tmp_path)
    ref = archive.put(b'{"schema_version":"x","schema_version":"y"}')
    with pytest.raises(ValueError, match="duplicate"):
        archive.read(ref)


def test_directory_durability_failure_leaves_no_published_object(tmp_path, monkeypatch):
    archive = MaterialArchive(tmp_path)
    import os
    import stat
    real = os.fsync
    def fail_directory(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("fixture directory sync failure")
        real(fd)
    monkeypatch.setattr(os, "fsync", fail_directory)
    with pytest.raises(OSError):
        archive.publish({"test": 1})
    assert not list(tmp_path.rglob("*.blob"))
    assert not list(tmp_path.rglob(".pending-*"))


def test_policy_registry_is_frozen(tmp_path):
    original = {"source:v1": lambda raw: {}}
    policy = ReviewedSourcePolicy("test_only", original)
    original.clear()
    assert "source:v1" in policy.validators
    with pytest.raises(TypeError):
        policy.validators["new"] = lambda raw: {}
