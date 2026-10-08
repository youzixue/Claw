"""Isolated SELECT-only evidence transactions, never the application writer session."""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date, datetime
import json
import math
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import event, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

SHANGHAI = ZoneInfo("Asia/Shanghai")
MAX_CELL_BYTES = 24 * 1024
MAX_LOCAL_RESPONSE_BYTES = 1024 * 1024


def local_now() -> datetime:
    return datetime.now(SHANGHAI).replace(tzinfo=None)


def cutoff(value=None) -> datetime:
    at = datetime.fromisoformat(value) if isinstance(value, str) else value
    at = at or local_now()
    if not isinstance(at, datetime):
        raise ValueError("explicit ISO date-time required")
    if at.tzinfo is not None:
        at = at.astimezone(SHANGHAI).replace(tzinfo=None)
    if at > local_now():
        raise ValueError("future as_of is not permitted")
    return at


def trade_day(value, *, as_of: datetime) -> date:
    day = date.fromisoformat(value) if isinstance(value, str) else value
    if type(day) is not date or day > as_of.date():
        raise ValueError("explicit non-future trade_date required")
    return day


def database_path(database_url=None) -> Path:
    from app.config.settings import settings
    url = make_url(database_url or settings.DATABASE_URL)
    if url.get_backend_name() != "sqlite" or not url.database:
        raise ValueError("local evidence requires existing SQLite storage")
    path = Path(url.database)
    if not path.is_absolute() or not path.is_file():
        raise ValueError("evidence database must already exist at an absolute path")
    return path.resolve()


@asynccontextmanager
async def evidence_session(database_url=None):
    """mode=ro + query_only + no autoflush + explicit stable read transaction.

    Never initialize storage, create an account, revalue NAV, refresh a cache or
    recover a missing table. The application engine is not used.
    """
    path = database_path(database_url)
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{path.as_uri()}?mode=ro&uri=true",
        connect_args={"timeout": 5},
    )

    @event.listens_for(engine.sync_engine, "connect")
    def set_readonly(connection, _):
        connection.execute("PRAGMA query_only=ON")

    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def reject_writes(conn, cursor, statement, parameters, context, executemany):
        word = statement.lstrip().split(None, 1)[0].upper() if statement.strip() else ""
        if word not in {"SELECT", "WITH", "BEGIN", "SAVEPOINT", "RELEASE", "ROLLBACK"}:
            raise RuntimeError("readonly_statement_denied")

    try:
        async with async_sessionmaker(engine, autoflush=False, expire_on_commit=False)() as db:
            await db.execute(text("BEGIN"))
            yield db
            # Session close rolls back the read transaction, never commits.
    finally:
        await engine.dispose()


def owned(value):
    """Finite JSON leaves; oversized cells are explicit omissions, not empty data."""
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, str) and len(value.encode()) > MAX_CELL_BYTES:
        import hashlib
        return {"status": "cell_omitted_byte_budget", "bytes": len(value.encode()),
                "sha256": hashlib.sha256(value.encode()).hexdigest()}
    if isinstance(value, dict):
        return {str(k): owned(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [owned(v) for v in value]
    return value


def record(row):
    return {column.name: owned(getattr(row, column.name))
            for column in row.__table__.columns}


def bounded(payload):
    payload = owned(payload)
    if len(json.dumps(payload, ensure_ascii=False, allow_nan=False, default=str).encode()) > MAX_LOCAL_RESPONSE_BYTES:
        raise ValueError("evidence response exceeds budget; use smaller page or a subsection")
    return payload
