#!/usr/bin/env python3
"""Replay point-in-time daily reviews over local trade dates.

Dry-run is the default. Pass --persist to append idempotent snapshots and audited
automation runs. This script never trains, promotes, or changes model parameters.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db.session import async_session
from app.review.automation import replay_daily_reviews
from app.review.service import REVIEW_PHASES


def _date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must be YYYY-MM-DD") from exc


async def _run(args) -> dict:
    async with async_session() as db:
        return await replay_daily_reviews(
            db,
            start_date=args.start_date,
            end_date=args.end_date,
            phases=args.phases,
            force=args.force,
            persist=args.persist,
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-date", required=True, type=_date)
    parser.add_argument("--end-date", required=True, type=_date)
    parser.add_argument(
        "--phases",
        nargs="+",
        choices=REVIEW_PHASES,
        default=list(REVIEW_PHASES),
    )
    parser.add_argument("--persist", action="store_true", help="append snapshots/runs; default is dry-run")
    parser.add_argument("--force", action="store_true", help="create a new automation attempt even if one succeeded")
    parser.add_argument("--json", action="store_true", help="print the full machine-readable result")
    args = parser.parse_args()
    result = asyncio.run(_run(args))
    if args.json:
        print(json.dumps(result, ensure_ascii=False, default=str, indent=2))
    else:
        statuses: dict[str, int] = {}
        for item in result["results"]:
            status = str(item.get("status") or "unknown")
            statuses[status] = statuses.get(status, 0) + 1
        print(
            f"range={result['start_date']}..{result['end_date']} "
            f"trade_days={result['trade_day_count']} executions={result['execution_count']} "
            f"persisted={result['persisted']} statuses={statuses}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
