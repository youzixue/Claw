"""Read-only D0 post-liquidation research; writes only a new outputs JSON."""
import argparse
import asyncio
from datetime import date, datetime
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))


async def run(args):
    from sqlalchemy import event, text
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from app.paper.post_exit_research import build_post_exit_report, PostExitPolicy
    now = datetime.now()
    if args.as_of.tzinfo is not None or args.as_of > now or args.start > args.end or args.end > args.as_of.date():
        raise ValueError("invalid local date/as_of range")
    output = args.output.resolve()
    if not output.is_relative_to((ROOT / "outputs").resolve()) or output.suffix != ".json" or output.exists():
        raise ValueError("output must be a new JSON under project outputs")
    if not output.parent.is_dir():
        raise ValueError("output parent must already exist")
    database, archive = args.database.resolve(), args.archive_root.resolve()
    if not database.is_file() or not archive.is_dir():
        raise ValueError("database and archive root must already exist")
    policy = PostExitPolicy(args.max_source_age_sec, args.max_sample_gap_sec)
    engine = create_async_engine(f"sqlite+aiosqlite:///{database.as_uri()}?mode=ro&uri=true")
    @event.listens_for(engine.sync_engine, "connect")
    def readonly(connection, _):
        connection.execute("PRAGMA query_only=ON")
    try:
        async with async_sessionmaker(engine, autoflush=False)() as db:
            await db.execute(text("BEGIN"))
            result = await build_post_exit_report(
                db, start_date=args.start, end_date=args.end, as_of=args.as_of,
                archive_root=archive, policy=policy,
            )
        encoded = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(output, flags, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(encoded)
        print(json.dumps({"output": str(output), "summary": result["summary"], "read_only": True}, ensure_ascii=False))
        return result
    finally:
        await engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--archive-root", type=Path, required=True)
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument("--as-of", type=datetime.fromisoformat, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-source-age-sec", type=int, default=180)
    parser.add_argument("--max-sample-gap-sec", type=int, default=90)
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
