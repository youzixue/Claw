"""Bounded SELECT-only calendar reader for reminder timing, not market evidence."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from datetime import date, timedelta

ROOT = Path("/Users/youzix/WorkBuddy/Claw")


def read_calendar(database: Path, start: date) -> dict:
    # mode=ro (not immutable) observes the live WAL without copying or syncing it.
    uri = database.resolve(strict=True).as_uri() + "?mode=ro"
    end = start + timedelta(days=370)
    with sqlite3.connect(uri, uri=True, timeout=2) as db:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        rows = db.execute(
            "SELECT trade_date,is_trade_day FROM trade_calendar "
            "WHERE trade_date >= ? AND trade_date <= ? ORDER BY trade_date LIMIT 372",
            (start.isoformat(), end.isoformat()),
        ).fetchall()
        db.rollback()
    if any(flag not in (0, 1) for _, flag in rows):
        raise ValueError("invalid calendar flag")
    digest = hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode()).hexdigest()
    return {"source": "stored_trade_calendar_no_sync", "read_only": True,
            "start": start.isoformat(), "end": end.isoformat(), "row_count": len(rows),
            "last_stored_date": rows[-1][0] if rows else None,
            "sha256": digest, "rows": [[day, bool(flag)] for day, flag in rows]}


if __name__ == "__main__":
    print(json.dumps(read_calendar(ROOT / "backend/claw.db", date.fromisoformat(sys.argv[1]))))
