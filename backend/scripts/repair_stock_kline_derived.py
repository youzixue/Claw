"""修复 THS 日K单根增量导致的昨收/涨跌幅脏数据。

只读预览疑似问题；旧 --apply 已禁用，必须通过隔离版本核查，不能按邻价覆盖历史。筛查范围：
- source='ths'
- change_pct=0 且 prev_close=open（旧单根派生逻辑特征）
- 能找到同股上一根有效收盘价
- 按相邻收盘价计算的涨跌幅处于 A 股合理日波动范围内
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=Path("claw.db"))
    parser.add_argument("--apply", action="store_true", help="旧参数保留用于明确拒绝；只支持只读预览")
    parser.add_argument("--max-change-pct", type=float, default=21.5)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    database = args.database.expanduser().resolve()
    if not database.is_file():
        raise SystemExit(f"数据库不存在: {database}")

    if args.apply:
        raise SystemExit("历史派生字段覆盖已禁用；请使用追加式K线采证并核查同源/复权/交易日证据")
    conn = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=60)
    conn.execute("PRAGMA query_only=ON")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=60000")
    rows = conn.execute(
        """
        SELECT
            current.id,
            current.code,
            current.trade_date,
            current.close,
            previous.close AS actual_prev_close
        FROM stock_kline AS current
        JOIN stock_kline AS previous
          ON previous.code = current.code
         AND previous.trade_date = (
             SELECT MAX(candidate.trade_date)
             FROM stock_kline AS candidate
             WHERE candidate.code = current.code
               AND candidate.trade_date < current.trade_date
         )
        WHERE current.source = 'ths'
          AND ABS(COALESCE(current.change_pct, 0)) < 0.000001
          AND ABS(COALESCE(current.prev_close, 0) - COALESCE(current.open, 0)) < 0.000001
          AND current.close > 0
          AND previous.close > 0
          AND ABS((current.close / previous.close - 1.0) * 100.0) BETWEEN 0.01 AND ?
        ORDER BY current.id
        """,
        (max(float(args.max_change_pct), 0.01),),
    ).fetchall()

    print(f"database={database}")
    print(f"repairable_rows={len(rows)}")
    if rows:
        print(
            "sample="
            + ", ".join(
                f"{row['code']}@{row['trade_date']}"
                for row in rows[:10]
            )
        )

    print("mode=read-only unverified_candidates; no price/return/calendar certification")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
