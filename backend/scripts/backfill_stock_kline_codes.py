"""按代码采集THS日K；默认预览，--apply只追加未审核版本，不覆盖/补写历史投影。"""

from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("codes", nargs="+", help="6 位股票代码")
    parser.add_argument("--database", type=Path, required=True, help="目标 SQLite 数据库")
    parser.add_argument("--apply", action="store_true", help="只追加未审核版本，不覆盖历史；缺省仅预览")
    return parser.parse_args()


async def main() -> int:
    args = parse_args()
    database = args.database.expanduser().resolve()
    if not database.is_file():
        raise SystemExit(f"数据库不存在: {database}")
    # settings 使用相对 SQLite 路径；维护脚本必须先固定为绝对路径，防止从不同 cwd 写错库。
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{database}"
    from app.data.kline_observations import persist_kline_observations
    from app.data.sources.ths_kline_source import ThsKlineSource
    from app.db.session import async_session

    codes = list(dict.fromkeys(str(code).strip() for code in args.codes))
    invalid_codes = [code for code in codes if len(code) != 6 or not code.isdigit()]
    if invalid_codes:
        raise SystemExit(f"股票代码格式错误: {', '.join(invalid_codes)}")

    source = ThsKlineSource()
    print(f"database={database}")
    records: list[dict] = []
    for code in codes:
        klines = await source.collect_init(code)
        print(
            f"code={code} rows={len(klines)} "
            f"range={klines[0]['trade_date'] if klines else '-'}~{klines[-1]['trade_date'] if klines else '-'}"
        )
        records.extend(klines)

    if not args.apply or not records:
        print("mode=dry-run" if not args.apply else "mode=apply no-op")
        return 0

    async with async_session() as session:
        result = await persist_kline_observations(session, records)
        await session.commit()
    print(f"mode=append-unreviewed observations={result['observations_appended']} "
          f"projection_writes={result['written']} historical_pit_eligible=False")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
