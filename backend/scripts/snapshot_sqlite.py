"""Exclusive private SQLite online snapshot; never opens the source for writing.

This is a consistent ONLINE backup, not a cold/start-time snapshot or PIT evidence.
Only the destination is converted to a standalone DELETE-journal database.
"""
import argparse
from datetime import datetime
import hashlib
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import time
import uuid


def file_sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def create_snapshot(source: Path, destination: Path, *, reserve_bytes=5 * 1024**3,
                    max_seconds=900.0, pages=1024):
    """Publish an exclusive 0600 file only after backup, quick_check and fsync.

    The caller owns a private 0700 destination directory. Failed partials are removed.
    No app imports, migrations, source journal settings or business requests occur.
    Time budget is cooperative, not a hard deadline for filesystem I/O.
    """
    source, destination = Path(source), Path(destination)
    if not source.is_absolute() or not destination.is_absolute():
        raise ValueError("source and destination must be explicit absolute paths")
    source = source.resolve(strict=True)
    if not source.is_file():
        raise ValueError("source must be an existing regular file")
    parent = destination.parent.resolve(strict=True)
    info = parent.stat()
    if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700 or info.st_uid != os.getuid():
        raise ValueError("destination directory must be owned by current user with mode 0700")
    destination = parent / destination.name
    if os.path.lexists(destination):
        raise FileExistsError(destination)
    if destination in {source, Path(str(source) + "-wal"), Path(str(source) + "-shm")}:
        raise ValueError("destination cannot replace source or its sidecars")
    if (type(reserve_bytes) is not int or reserve_bytes < 0 or type(pages) is not int or pages <= 0
            or type(max_seconds) not in (int, float) or not 0 < max_seconds < float("inf")):
        raise ValueError("invalid snapshot resource budget")
    partial = parent / (destination.name + ".partial-" + uuid.uuid4().hex)
    started = time.monotonic()
    started_at = datetime.now().isoformat()
    reader = writer = None
    progress_calls = 0
    try:
        reader = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=5)
        reader.execute("PRAGMA query_only=ON")
        assert reader.execute("PRAGMA query_only").fetchone()[0] == 1
        estimated_bytes = reader.execute("PRAGMA page_count").fetchone()[0] * reader.execute("PRAGMA page_size").fetchone()[0]
        if shutil.disk_usage(parent).free < estimated_bytes + reserve_bytes:
            raise OSError("insufficient free space for snapshot plus reserve")
        fd = os.open(partial, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        writer = sqlite3.connect(partial.as_uri() + "?mode=rw", uri=True, timeout=5)

        def progress(status, remaining, total):
            nonlocal progress_calls
            progress_calls += 1
            if time.monotonic() - started > max_seconds:
                raise TimeoutError("snapshot exceeded cooperative time budget")

        reader.backup(writer, pages=pages, progress=progress, sleep=0.02)
        reader.close()
        reader = None
        # Change only the owned completed destination, never checkpoint the source.
        mode = writer.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
        if mode != "delete":
            raise RuntimeError("snapshot did not become standalone")
        writer.set_progress_handler(lambda: int(time.monotonic() - started > max_seconds), 10000)
        check = [row[0] for row in writer.execute("PRAGMA quick_check")]
        if check != ["ok"]:
            raise RuntimeError("snapshot quick_check failed")
        writer.close()
        writer = None
        if any(Path(str(partial) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
            raise RuntimeError("snapshot still has journal sidecars")
        with partial.open("rb") as handle:
            os.fsync(handle.fileno())
        digest = file_sha256(partial)
        size = partial.stat().st_size
        # link() is exclusive even if another caller creates destination meanwhile.
        os.link(partial, destination)
        partial.unlink()
        directory_fd = os.open(parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return {
            "protocol": "claw_sqlite_online_snapshot_v1", "source": str(source),
            "destination": str(destination), "source_open_mode": "ro",
            "source_query_only": True, "started_at": started_at,
            "completed_at": datetime.now().isoformat(),
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "estimated_source_bytes": estimated_bytes, "bytes": size, "sha256": digest,
            "quick_check": check, "standalone_journal_mode": mode,
            "progress_calls": progress_calls, "cold_backup": False,
            "point_in_time_verified": False,
        }
    finally:
        if reader is not None:
            reader.close()
        if writer is not None:
            writer.close()
        # These randomized names were exclusively created inside the private directory.
        for suffix in ("", "-wal", "-shm", "-journal"):
            item = Path(str(partial) + suffix)
            if item.exists():
                item.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--reserve-gib", type=int, default=5)
    parser.add_argument("--max-seconds", type=float, default=900)
    args = parser.parse_args()
    import json
    result = create_snapshot(args.source, args.destination,
                             reserve_bytes=args.reserve_gib * 1024**3, max_seconds=args.max_seconds)
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))


if __name__ == "__main__":
    main()
