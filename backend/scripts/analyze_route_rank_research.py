"""Read-only frozen C-route ranking comparison; prints JSON, never writes a DB."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy.exc import SQLAlchemyError

from app.promotion.route_rank_research import RouteQuotaContract, analyze_database


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--run-id", type=int, required=True, help="Exact frozen run; never falls back")
    parser.add_argument("--decision-at", type=datetime.fromisoformat, required=True,
                        help="Explicit naive Asia/Shanghai cutoff")
    parser.add_argument("--quote-as-of", type=datetime.fromisoformat)
    parser.add_argument("--formal-quota", type=int, required=True)
    parser.add_argument("--recall-quota", type=int, required=True,
                        help="Reserved C minimum including formal slots; not extra slots")
    args = parser.parse_args()
    try:
        result = asyncio.run(analyze_database(
            args.database, run_id=args.run_id, decision_at=args.decision_at,
            quote_as_of=args.quote_as_of,
            contract=RouteQuotaContract(args.formal_quota, args.recall_quota),
        ))
    except (ValueError, OSError, SQLAlchemyError) as exc:
        print(json.dumps({"status": "rejected", "reason": str(exc), "buy_allowed": False}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
