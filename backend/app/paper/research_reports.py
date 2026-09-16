"""Independent read-only daily research publication; never uses the trading session."""
import asyncio
from datetime import date, datetime, time
import hashlib
import json
import os
from pathlib import Path
import uuid

from sqlalchemy import event, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config.settings import settings
from app.models.governance import TradeCalendarModel

ROOT = Path(__file__).resolve().parents[3]


def _database_path(database_url):
    url = make_url(database_url)
    if url.drivername not in {"sqlite", "sqlite+aiosqlite"} or url.query or not url.database:
        raise ValueError("research publication requires an existing local SQLite file")
    path = Path(url.database)
    if not path.is_absolute() or not path.is_file():
        raise ValueError("research database must be an existing absolute file")
    return path.resolve()


def _publish(result, directory, at):
    """Prepare fully before atomically publishing; never overwrite a past report."""
    content = (json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2,
                          allow_nan=False) + "\n").encode()
    digest = hashlib.sha256(content).hexdigest()
    directory.mkdir(parents=True, exist_ok=True)
    name = f"paper-research-{at:%Y%m%dT%H%M%S%f}-{digest[:16]}.json"
    destination = directory / name
    temporary = directory / f".{uuid.uuid4().hex}.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, destination)
        except FileExistsError:
            if destination.read_bytes() != content:
                raise ValueError("immutable research report collision")
    finally:
        temporary.unlink(missing_ok=True)
    return {"output": str(destination), "sha256": digest}


async def publish_daily_paper_research(*, now=None, database_url=None, output_dir=None):
    """Scheduled 15:50/20:45; calendar is read, never fetched or repaired here."""
    from app.paper.signal_research import build_parallel_research_report
    from app.paper.post_exit_research import build_post_exit_report

    if not settings.PAPER_CONTINUOUS_EXPERIMENT_ENABLED:
        return {"status": "disabled", "read_only": True}
    at = now or datetime.now()
    if not isinstance(at, datetime) or at.tzinfo is not None or at > datetime.now():
        raise ValueError("research as_of must be a non-future Shanghai local clock")
    if at.time() < time(15, 45):
        return {"status": "before_close_review", "read_only": True}
    start = date.fromisoformat(settings.PAPER_EXPERIMENT_START_DATE)
    if start > at.date():
        return {"status": "before_experiment", "read_only": True}
    directory = Path(output_dir or ROOT / "outputs/paper_research").resolve()
    if not directory.is_relative_to((ROOT / "outputs").resolve()):
        raise ValueError("research output must stay under project outputs")
    database = _database_path(database_url or settings.DATABASE_URL)
    engine = create_async_engine(f"sqlite+aiosqlite:///{database.as_uri()}?mode=ro&uri=true")

    @event.listens_for(engine.sync_engine, "connect")
    def readonly(connection, _):
        connection.execute("PRAGMA query_only=ON")

    try:
        async with async_sessionmaker(engine, autoflush=False)() as db:
            await db.execute(text("BEGIN"))
            is_day = await db.scalar(select(TradeCalendarModel.is_trade_day).where(
                TradeCalendarModel.trade_date == at.date()))
            if is_day is not True:
                return {"status": "calendar_unknown" if is_day is None else "non_trading_day",
                        "read_only": True, "as_of": at.isoformat()}
            sections = {}
            # SAVEPOINT protects independent sections from a read query error.
            # This is a separate query_only connection, never the trading session.
            for key, builder, kwargs in (
                ("signal_portfolio", build_parallel_research_report,
                 {"start_date": start, "end_date": at.date(), "as_of": at}),
                ("post_exit", build_post_exit_report,
                 {"start_date": at.date(), "end_date": at.date(), "as_of": at,
                  "archive_root": settings.QUOTE_ROUND_ARCHIVE_DIR}),
            ):
                try:
                    async with db.begin_nested():
                        report = await builder(db, **kwargs)
                    sections[key] = {"status": "built", "report": report}
                except Exception as exc:
                    # Do not expose SQL/paths/credentials or count failure as zero samples.
                    sections[key] = {"status": "unavailable", "error_type": type(exc).__name__,
                                     "report": None}
        result = {"schema": "paper_daily_research_v1", "as_of": at.isoformat(),
                  "read_only": True, "database_snapshot": "single_explicit_read_transaction",
                  "report_availability": "generated_now_not_historically_reconstructed",
                  "sections": sections}
        published = await asyncio.to_thread(_publish, result, directory, at)
        return {"status": "published" if all(s["status"] == "built" for s in sections.values()) else "partial",
                "read_only": True, "as_of": at.isoformat(), **published,
                "sections": {key: value["status"] for key, value in sections.items()}}
    finally:
        await engine.dispose()
