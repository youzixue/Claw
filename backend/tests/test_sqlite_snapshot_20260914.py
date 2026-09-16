"""Snapshot safety tests use only owned temporary databases."""
import hashlib
import os
from pathlib import Path
import sqlite3

import pytest

from scripts.snapshot_sqlite import create_snapshot


@pytest.fixture
def files(tmp_path):
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    source = tmp_path / "source.sqlite"
    with sqlite3.connect(source) as db:
        db.execute("CREATE TABLE evidence(id INTEGER PRIMARY KEY, value TEXT)")
        db.execute("INSERT INTO evidence VALUES(1, 'committed')")
    return source, root / "copy.sqlite"


def read_rows(path):
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
        return db.execute("SELECT * FROM evidence ORDER BY id").fetchall()


def test_wal_commits_included_uncommitted_writer_excluded_and_source_untouched(files):
    source, output = files
    db = sqlite3.connect(source)
    try:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("INSERT INTO evidence VALUES(2, 'wal-committed')")
        db.commit()
        before_main = hashlib.sha256(source.read_bytes()).hexdigest()
        before_wal = hashlib.sha256(Path(str(source) + "-wal").read_bytes()).hexdigest()
        db.execute("INSERT INTO evidence VALUES(3, 'not-committed')")
        report = create_snapshot(source, output, reserve_bytes=0)
        assert read_rows(output) == [(1, "committed"), (2, "wal-committed")]
        assert report["quick_check"] == ["ok"] and report["cold_backup"] is False
        assert report["point_in_time_verified"] is False
        assert report["sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
        assert output.stat().st_mode & 0o777 == 0o600
        assert hashlib.sha256(source.read_bytes()).hexdigest() == before_main
        assert hashlib.sha256(Path(str(source) + "-wal").read_bytes()).hexdigest() == before_wal
        assert not list(output.parent.glob("*.partial-*"))
        assert not Path(str(output) + "-wal").exists()
        db.rollback()
        assert read_rows(source) == read_rows(output)
    finally:
        db.close()


def test_existing_destination_not_truncated(files):
    source, output = files
    output.write_bytes(b"must stay")
    with pytest.raises(FileExistsError):
        create_snapshot(source, output, reserve_bytes=0)
    assert output.read_bytes() == b"must stay"


def test_symlink_not_followed_even_if_broken(files):
    source, output = files
    output.symlink_to(output.parent / "missing")
    with pytest.raises(FileExistsError):
        create_snapshot(source, output, reserve_bytes=0)
    assert output.is_symlink() and not output.exists()


@pytest.mark.parametrize("mode", [0o755, 0o750, 0o777])
def test_nonprivate_destination_directory_refused(files, mode):
    source, output = files
    output.parent.chmod(mode)
    with pytest.raises(ValueError, match="0700"):
        create_snapshot(source, output, reserve_bytes=0)
    assert not output.exists()


def test_missing_source_not_created(files):
    source, output = files
    source = source.with_name("absent.sqlite")
    with pytest.raises(FileNotFoundError):
        create_snapshot(source, output, reserve_bytes=0)
    assert not source.exists() and not output.exists()


@pytest.mark.parametrize("kwargs", [
    {"reserve_bytes": -1}, {"reserve_bytes": True}, {"pages": 0},
    {"pages": True}, {"max_seconds": float("nan")}, {"max_seconds": 0},
])
def test_bad_budgets_refused(files, kwargs):
    source, output = files
    with pytest.raises(ValueError):
        create_snapshot(source, output, **kwargs)
    assert not output.exists()


def test_no_space_fails_before_partial_creation(files, monkeypatch):
    source, output = files
    from types import SimpleNamespace
    monkeypatch.setattr("scripts.snapshot_sqlite.shutil.disk_usage", lambda _: SimpleNamespace(free=0))
    with pytest.raises(OSError, match="space"):
        create_snapshot(source, output, reserve_bytes=0)
    assert list(output.parent.iterdir()) == []


def test_timeout_never_publishes_partial(files):
    source, output = files
    with pytest.raises(TimeoutError):
        create_snapshot(source, output, max_seconds=1e-12, reserve_bytes=0)
    assert list(output.parent.iterdir()) == []
    assert read_rows(source) == [(1, "committed")]


def test_corrupt_source_not_published(files):
    source, output = files
    source.write_bytes(b"not a database")
    with pytest.raises(sqlite3.DatabaseError):
        create_snapshot(source, output, reserve_bytes=0)
    assert list(output.parent.iterdir()) == []


def test_relative_path_refused(files):
    source, output = files
    with pytest.raises(ValueError, match="absolute"):
        create_snapshot(Path("relative.sqlite"), output, reserve_bytes=0)


def test_two_standalone_copies_equal_then_independent(files):
    source, output = files
    create_snapshot(source, output, reserve_bytes=0)
    restore = output.with_name("restore.sqlite")
    create_snapshot(output, restore, reserve_bytes=0)
    assert read_rows(source) == read_rows(output) == read_rows(restore)
    with sqlite3.connect(restore) as db:
        db.execute("INSERT INTO evidence VALUES(2, 'restore-only')")
    assert read_rows(output) == read_rows(source) == [(1, "committed")]
    assert len(read_rows(restore)) == 2
