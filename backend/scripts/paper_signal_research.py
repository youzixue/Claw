"""只读12账户全量确认研究；不会创建账户、回填事件、结算表或发送消息。"""
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
    from app.paper.signal_research import build_parallel_research_report, MarkoutPolicy
    now = datetime.now()
    output = args.output.resolve()
    if not output.is_relative_to(ROOT / "outputs") or output.suffix != ".json" or output.exists():
        raise ValueError("output must be a new JSON under project outputs")
    if args.start > args.end or args.end > now.date():
        raise ValueError("invalid date range")
    database = args.database.resolve()
    if not database.is_file():
        raise ValueError("database must already exist")
    policy = MarkoutPolicy(quantity=args.quantity)
    engine = create_async_engine(f"sqlite+aiosqlite:///{database.as_uri()}?mode=ro&uri=true")
    @event.listens_for(engine.sync_engine, "connect")
    def readonly(connection, _):
        connection.execute("PRAGMA query_only=ON")
    try:
        async with async_sessionmaker(engine, autoflush=False)() as db:
            # SQLite legacy transaction mode does not begin on SELECT by itself.
            # Freeze one read snapshot across logs/orders/fills/outcome queries.
            await db.execute(text("BEGIN"))
            result = await build_parallel_research_report(db, start_date=args.start,
                end_date=args.end, as_of=now, policy=policy)
        with output.open("x", encoding="utf-8") as stream:
            os.chmod(output, 0o600)
            json.dump(result, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
        print(json.dumps({"output": str(output), "summary": result["summary"],
                          "account_count": len(result["accounts"]), "read_only": True}, ensure_ascii=False))
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=ROOT / "backend/claw.db")
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--quantity", type=int, default=100, help="explicit hypothetical whole-lot size, not actual orders")
    asyncio.run(run(parser.parse_args()))
