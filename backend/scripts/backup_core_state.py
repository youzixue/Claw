#!/usr/bin/env python3
"""Claw 核心状态精简备份：只备份「重建不了」的表，跳过可重采的原始行情。

为什么需要这个
--------------
`backend/claw.db` 16.81 GB，但其中约 90% 是**可以重新从行情源采集**的原始数据
（`stock_kline` 875 万行、`auction_data` 156 万行、`fund_flow` 47 万行）。
真正**重建不了**的是系统自身的决策与账本：
模拟盘账本、影子路线事件、晋级预测历史、K 线观察证据……
这些一旦丢失，无法从任何外部数据源恢复。

所以本脚本采用**白名单式排除**：只跳过明确属于「纯行情原始数据」的少数几张表，
**其余全部备份**。宁可多备，不可漏备。

安全设计
--------
* 源库以 `mode=ro` + `query_only=ON` 打开，**绝不可能写源库**（与 `snapshot_sqlite.py` 同姿态）。
* 目标库：已存在则拒绝（不覆盖），除非显式 `--overwrite`。
* 备份完成后对目标做 `integrity_check`，失败则报错并非零退出。
* 仅目标库做 `VACUUM`，源库不受影响。
* `--gzip` 对目标做 gzip 归档（默认开启），归档后删除中间 sqlite（除非 `--keep-sqlite`）。

用法
----
    # 默认：备份到 outputs/backups/core-state-<日期>.sqlite.gz
    python3 scripts/backup_core_state.py

    # 指定目标 + 保留中间 sqlite
    python3 scripts/backup_core_state.py --destination /path/to/backup.sqlite --keep-sqlite
"""
from __future__ import annotations

import argparse
import gzip
import shutil
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
DEFAULT_SOURCE = REPO / "backend" / "claw.db"

# 只排除**纯行情原始数据**（可从腾讯/同花顺/东财等重新采集）。
# 判据：该表内容完全来自外部行情源，且系统不做任何自有决策记录。
# 注意：sector_* / limit_*_pool 等虽由行情派生，但含系统口径的筛选结果，
# 重采成本高且口径可能漂移，故**不排除**。
EXCLUDED_TABLES = (
    "stock_kline",      # 8,756,257 行 —— OHLCV 原始日K，可完整重采
    "auction_data",     # 1,556,431 行 —— 竞价原始快照，可重采
    "fund_flow",        #   475,059 行 —— 资金流原始数据，可重采
    "sector_kline",     #   155,663 行 —— 板块K线，由成分股聚合，可重算
)

EXPECTED_SCHEMA_VERSION = None   # 不强制；仅记录实际值


def _connect_readonly(path: Path) -> sqlite3.Connection:
    """以只读 + query_only 打开，杜绝任何写源库的可能。"""
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    db.execute("PRAGMA query_only=ON")
    if db.execute("PRAGMA query_only").fetchone()[0] != 1:
        raise RuntimeError("query_only 未能生效，拒绝继续")
    return db


def _table_names(db: sqlite3.Connection) -> list[str]:
    return [
        r[0]
        for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]


def _ddl_for(db: sqlite3.Connection, name: str) -> str | None:
    row = db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row[0] if row and row[0] else None


def _index_ddl(db: sqlite3.Connection, table: str) -> list[str]:
    return [
        r[0]
        for r in db.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=? "
            "AND sql IS NOT NULL",
            (table,),
        )
    ]


def run(source: Path, destination: Path, *, overwrite: bool, do_gzip: bool,
        keep_sqlite: bool, quiet: bool) -> int:
    if not source.is_file():
        print(f"源库不存在：{source}", file=sys.stderr)
        return 2

    work = destination.with_suffix(".partial.sqlite")
    for candidate in (destination, work):
        if candidate.exists():
            if not overwrite:
                print(f"目标已存在，拒绝覆盖：{candidate}", file=sys.stderr)
                return 2
            candidate.unlink()

    destination.parent.mkdir(parents=True, exist_ok=True)

    src = _connect_readonly(source)
    src_version = src.execute("PRAGMA user_version").fetchone()[0]
    try:
        alembic = src.execute(
            "SELECT version_num FROM alembic_version LIMIT 1"
        ).fetchone()
    except sqlite3.Error:
        alembic = None

    all_tables = _table_names(src)
    excluded = [t for t in all_tables if t in EXCLUDED_TABLES]
    keep = [t for t in all_tables if t not in EXCLUDED_TABLES]

    started = time.time()
    # 目标连接必须开启 uri 支持，才能把源库以 mode=ro 的方式 ATTACH 进来
    # （只读 ATTACH 是「绝不可能写源库」的第二重保证）。
    dst = sqlite3.connect(work.as_uri() + "?mode=rwc", uri=True)
    try:
        dst.execute("PRAGMA journal_mode=DELETE")
        dst.execute("ATTACH DATABASE ? AS src", (source.as_uri() + "?mode=ro",))
        # 不要在这里 `PRAGMA src.query_only=ON`：query_only 是**连接级**开关，
        # 会把 main（目标库）一起锁成只读，导致后续 CREATE TABLE 报
        # "attempt to write a readonly database"。源库只读由 ATTACH 的
        # `?mode=ro` 在打开时就保证，无需也不会用 PRAGMA 二次设置。

        rows_total = 0
        copied: list[tuple[str, int]] = []
        for table in keep:
            ddl = _ddl_for(src, table)
            if not ddl:
                continue
            dst.execute(ddl)
            cols = [r[1] for r in src.execute(f'PRAGMA table_info("{table}")')]
            if cols:
                collist = ", ".join(f'"{c}"' for c in cols)
                dst.execute(
                    f'INSERT INTO main."{table}" ({collist}) '
                    f'SELECT {collist} FROM src."{table}"'
                )
            # 逐表核对：目标行数必须等于源行数，否则视为备份失败。
            # （首版因为 SELECT 忘了指向 src、写成了 main，静默复制了 0 行，
            #   而 integrity_check 对空库照样返回 ok —— 这个断言就是为了堵住它。）
            n = src.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
            m = dst.execute(f'SELECT count(*) FROM main."{table}"').fetchone()[0]
            if n != m:
                print(f"行数不一致 {table}: 源={n} 目标={m}", file=sys.stderr)
                return 3
            rows_total += m
            copied.append((table, m))
            for idx in _index_ddl(src, table):
                try:
                    dst.execute(idx)
                except sqlite3.Error:
                    pass          # 索引名冲突/触发器式索引，非致命
        dst.commit()

        check = dst.execute("PRAGMA integrity_check").fetchone()[0]
        if check != "ok":
            print(f"目标库完整性检查失败：{check}", file=sys.stderr)
            return 3
        if rows_total == 0:
            print("备份 0 行，拒绝发布", file=sys.stderr)
            return 3
        dst.execute("VACUUM")
        dst.commit()
    finally:
        dst.close()
        src.close()

    elapsed = time.time() - started
    size = work.stat().st_size

    if do_gzip:
        gz = destination if destination.suffix == ".gz" else destination.with_suffix(
            destination.suffix + ".gz"
        )
        with work.open("rb") as fin, gzip.open(gz, "wb", compresslevel=6) as fout:
            shutil.copyfileobj(fin, fout, length=4 * 1024 * 1024)
        gz_size = gz.stat().st_size
        if not keep_sqlite:
            work.unlink()
        final = gz
    else:
        final = work
        work.replace(destination)
        final = destination
        gz_size = None

    if not quiet:
        print(f"  源库      {source}")
        print(f"  源 schema user_version={src_version} alembic={alembic[0] if alembic else 'n/a'}")
        print(f"  备份表    {len(copied)} 张 / {rows_total:,} 行")
        print(f"  排除表    {len(excluded)} 张（纯行情原始数据，可重采）：{', '.join(excluded)}")
        print(f"  中间库    {size/1048576:.0f} MB")
        if gz_size is not None:
            print(f"  归档      {final.name}  {gz_size/1048576:.0f} MB")
        print(f"  耗时      {elapsed:.1f}s   integrity_check=ok")
    else:
        print(f"backup ok: {final} {rows_total} rows, "
              f"{(gz_size or size)/1048576:.0f} MB, {elapsed:.1f}s")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Claw 核心状态精简备份")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--destination", type=Path, default=None)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--no-gzip", action="store_true")
    parser.add_argument("--keep-sqlite", action="store_true",
                        help="gzip 后保留中间 sqlite（默认删除）")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    destination = args.destination
    if destination is None:
        stamp = datetime.now().strftime("%Y%m%d")
        destination = REPO / "outputs" / "backups" / f"core-state-{stamp}.sqlite.gz"

    return run(
        args.source.resolve(),
        destination.resolve(),
        overwrite=args.overwrite,
        do_gzip=not args.no_gzip,
        keep_sqlite=args.keep_sqlite,
        quiet=args.quiet,
    )


if __name__ == "__main__":
    raise SystemExit(main())
