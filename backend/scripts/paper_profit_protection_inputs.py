"""Read-only evidence inventory for profit protection; never invent frozen replay."""
import argparse
import asyncio
from collections import Counter
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
    from app.paper.profit_protection_inputs import build_profit_protection_inputs
    from app.paper.profit_protection_research import ProfitProtectionPolicy
    if (args.as_of.tzinfo is not None or args.as_of > datetime.now()
            or args.start > args.end or args.end > args.as_of.date()):
        raise ValueError("invalid explicit local research cutoff")
    output = args.output.resolve()
    if (not output.is_relative_to((ROOT / "outputs").resolve()) or output.suffix != ".json"
            or output.exists() or not output.parent.is_dir()):
        raise ValueError("output must be a new JSON in existing project outputs")
    database, archive = args.database.resolve(), args.archive_root.resolve()
    if not database.is_file() or not archive.is_dir():
        raise ValueError("existing database and archive root required")
    policy = ProfitProtectionPolicy(args.policy_version, args.activation_profit_pct,
        args.pullback_from_peak_pct, args.max_source_age_sec, args.max_sample_gap_sec)
    engine = create_async_engine(f"sqlite+aiosqlite:///{database.as_uri()}?mode=ro&uri=true")
    @event.listens_for(engine.sync_engine, "connect")
    def readonly(connection, _):
        connection.execute("PRAGMA query_only=ON")
    try:
        async with async_sessionmaker(engine, autoflush=False)() as db:
            await db.execute(text("BEGIN"))
            result = await build_profit_protection_inputs(db, start_date=args.start, end_date=args.end,
                as_of=args.as_of, archive_root=archive, policies=(policy,))
        encoded = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(encoded)
        print(json.dumps({"output": str(output), "status": result["status"],
            "audited_positions": len(result["position_audits"]),
            "frozen_positions": len(result["positions"]),
            "block_reasons": dict(Counter(reason for p in result["position_audits"] for reason in p["reasons"])),
            "read_only": True, "replay_performed": False}, ensure_ascii=False))
        return result
    finally:
        await engine.dispose()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--database", type=Path, required=True)
    p.add_argument("--archive-root", type=Path, required=True)
    p.add_argument("--start", type=date.fromisoformat, required=True)
    p.add_argument("--end", type=date.fromisoformat, required=True)
    p.add_argument("--as-of", type=datetime.fromisoformat, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--policy-version", required=True)
    p.add_argument("--activation-profit-pct", type=float, required=True)
    p.add_argument("--pullback-from-peak-pct", type=float, required=True)
    p.add_argument("--max-source-age-sec", type=int, default=90)
    p.add_argument("--max-sample-gap-sec", type=int, default=90)
    asyncio.run(run(p.parse_args()))


if __name__ == "__main__":
    main()
