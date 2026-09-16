#!/usr/bin/env python3
"""从本地只读 SQLite 快照导出单日上涨股与涨停池，供盘后审计。"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sqlite3
from datetime import date
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "backend" / "claw.db"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trade-date", required=True, type=date.fromisoformat)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--output-dir", type=Path)
    return parser.parse_args()


def _write_csv(path: Path, rows: list[sqlite3.Row]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(rows[0].keys()) if rows else []
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        if fieldnames:
            writer.writeheader()
            writer.writerows(dict(row) for row in rows)


def main() -> None:
    args = _parse_args()
    trade_date = args.trade_date.isoformat()
    output_dir = args.output_dir or ROOT / "outputs" / f"daily_review_{args.trade_date:%Y%m%d}"
    db_path = args.db.resolve()
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row

    rising_rows = connection.execute(
        """
        SELECT
            k.code,
            COALESCE(s.name, t.name, k.code) AS name,
            ROUND(k.change_pct, 2) AS change_pct,
            k.open,
            k.high,
            k.low,
            k.close,
            ROUND(COALESCE(k.amount, 0) / 100000000.0, 4) AS amount_yi,
            ROUND(COALESCE(k.turnover, s.turnover), 2) AS turnover_pct,
            t.board_type,
            t.board_tag,
            COALESCE(t.is_st, 0) AS is_st,
            CASE WHEN p.code IS NULL THEN 0 ELSE 1 END AS in_limit_up_pool,
            p.consecutive_days,
            p.limit_up_time,
            ROUND(COALESCE(p.seal_amount, 0) / 100000000.0, 2) AS seal_amount_yi,
            p.break_count,
            p.limit_up_reason
        FROM stock_kline AS k
        LEFT JOIN stock_spot AS s ON s.code = k.code
        LEFT JOIN stock_tags AS t ON t.code = k.code
        LEFT JOIN limit_up_pool AS p
          ON p.code = k.code
         AND p.trade_date = k.trade_date
         AND COALESCE(p.quarantined, 0) = 0
        WHERE k.trade_date = ?
          AND k.change_pct > 0
        ORDER BY k.change_pct DESC, k.amount DESC, k.code
        """,
        (trade_date,),
    ).fetchall()

    limit_rows = connection.execute(
        """
        SELECT
            p.code,
            p.name,
            p.consecutive_days,
            p.limit_up_time,
            ROUND(COALESCE(p.seal_amount, 0) / 100000000.0, 2) AS seal_amount_yi,
            p.break_count,
            ROUND(p.turnover, 2) AS turnover_pct,
            p.limit_up_reason,
            t.board_type,
            t.board_tag,
            COALESCE(t.is_st, 0) AS is_st,
            ROUND(k.change_pct, 2) AS close_change_pct,
            k.close,
            p.source
        FROM limit_up_pool AS p
        LEFT JOIN stock_kline AS k
          ON k.code = p.code
         AND k.trade_date = p.trade_date
        LEFT JOIN stock_tags AS t ON t.code = p.code
        WHERE p.trade_date = ?
          AND COALESCE(p.quarantined, 0) = 0
        ORDER BY p.consecutive_days DESC, p.seal_amount DESC, p.code
        """,
        (trade_date,),
    ).fetchall()

    hasher = hashlib.sha256()
    for label, rows in (("rising", rising_rows), ("limit_up", limit_rows)):
        hasher.update(label.encode())
        for row in rows:
            hasher.update(
                json.dumps(
                    tuple(row),
                    ensure_ascii=False,
                    separators=(",", ":"),
                    default=str,
                ).encode()
            )
    data_version = f"daily_export_{hasher.hexdigest()[:12]}"
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(output_dir / f"上涨个股_{args.trade_date:%Y%m%d}.csv", rising_rows)
    _write_csv(output_dir / f"涨停池_{args.trade_date:%Y%m%d}.csv", limit_rows)
    metadata = {
        "trade_date": trade_date,
        "database": str(db_path),
        "data_version": data_version,
        "rising_count": len(rising_rows),
        "limit_up_count": len(limit_rows),
        "scope": "Claw 本地交易覆盖口径；上涨以当日 StockKline.change_pct > 0 为准，涨停以未隔离 LimitUpPool 为准",
    }
    (output_dir / "metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(metadata, ensure_ascii=False))


if __name__ == "__main__":
    main()
