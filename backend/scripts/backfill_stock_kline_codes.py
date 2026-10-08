"""THS历史日K：--download保存独立看图文件；旧--apply仅追加未审核数据库版本。"""

from __future__ import annotations

import argparse
import asyncio
from datetime import date
import hashlib
import json
import os
from pathlib import Path
import uuid


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("codes", nargs="*", help="6位股票代码，用空格分隔")
    parser.add_argument("--database", type=Path, help="旧归档模式的目标SQLite库（必须已建表）")
    parser.add_argument("--apply", action="store_true", help="旧模式仅追加未审核版本，不覆盖历史")
    parser.add_argument("--download", action="store_true", help="新电脑：下载独立历史看图文件，不访问数据库")
    parser.add_argument("--all", action="store_true", help="下载交易所当前沪深京A股列表；不是历史证券池")
    parser.add_argument("--start-date", type=date.fromisoformat, default=date(2018, 1, 1))
    parser.add_argument("--end-date", type=date.fromisoformat, help="默认本次下载的上海日期")
    parser.add_argument("--limit", type=int, help="只处理前N只，用于小批试跑")
    parser.add_argument("--refresh", action="store_true", help="重新抓取，仍保留旧文件；默认跳过同范围已有有效文件")
    parser.add_argument("--status", action="store_true", help="只查看最近下载进度，不联网")
    args = parser.parse_args(argv)
    if args.download:
        if args.database or args.apply:
            parser.error("--download不能与--database/--apply混用")
        if not args.status and (bool(args.codes) == args.all):
            parser.error("--download需要股票代码或--all，二者只能选一个")
    elif not args.codes or not args.database or args.all or args.status or args.refresh or args.limit:
        parser.error("旧归档模式需要股票代码与--database；新电脑请使用--download")
    if args.limit is not None and args.limit < 1:
        parser.error("--limit必须大于0")
    if any(len(code) != 6 or not code.isascii() or not code.isdigit() for code in args.codes):
        parser.error("股票代码必须是6位数字")
    return args


async def download_history(args, *, source=None, root=None, now=None) -> int:
    """Offline chart bootstrap. No scheduler, account, DB init or projection writes."""
    from app.config.settings import settings
    from app.data.kline_observations import (
        DOWNLOAD_PROTOCOL, load_kline_download, save_kline_download, write_download_json,
    )
    from app.data.sources.after_hours_source import SHANGHAI, local_now

    def download_now():
        # The shared source clock is naive Shanghai for DB consumers; files carry its zone.
        return local_now().replace(tzinfo=SHANGHAI)

    root = Path(root or settings.KLINE_DOWNLOAD_DIR).expanduser().resolve()
    progress_path = root / "progress.json"
    if args.status:
        if not progress_path.is_file():
            print("尚无历史下载记录")
        else:
            print(progress_path.read_text(encoding="utf-8"))
        return 0
    now = now or download_now()
    start, end = args.start_date, args.end_date or now.date()
    if not date(2018, 1, 1) <= start <= end <= now.date():
        raise ValueError("日期必须满足2018-01-01 <= 开始 <= 结束 <= 上海当前日期")

    if args.all:
        universe_path = root / "universe.json"
        if universe_path.is_file() and not args.refresh:
            universe = json.loads(universe_path.read_text(encoding="utf-8"))
            if universe.get("scope") != "current_listed_not_historical_universe":
                raise ValueError("股票列表缓存无效；请用--refresh重新获取")
            codes = universe["codes"]
        else:
            from app.data.sources.akshare_source import AkShareSource
            frame = await AkShareSource().get_stock_list()
            if frame is None or frame.empty or not {"code", "name"}.issubset(frame.columns):
                raise ValueError("交易所股票列表不可用，未开始下载")
            codes = sorted(set(str(code).strip() for code in frame["code"]))
            universe = {
                "scope": "current_listed_not_historical_universe",
                "fetched_at": now.isoformat(), "codes": codes,
                "source": "akshare.stock_info_a_code_name",
            }
            # Validate before publishing the universe below.
    else:
        codes = list(dict.fromkeys(args.codes))
        universe = None
    if not codes or any(not isinstance(code, str) or len(code) != 6
                        or not code.isascii() or not code.isdigit() for code in codes):
        raise ValueError("股票列表为空或包含非6位股票代码，未开始下载")
    if args.all:
        write_download_json(root / "universe.json", universe)
    universe_count = len(codes)
    if args.limit:
        codes = codes[:args.limit]

    if source is None:
        from app.data.sources.ths_kline_source import ThsKlineSource
        source = ThsKlineSource()
    identity = json.dumps([codes, start.isoformat(), end.isoformat()], separators=(",", ":"))
    request_id = hashlib.sha256(identity.encode()).hexdigest()[:16]
    attempt_id = uuid.uuid4().hex
    request_path = root / f"progress-{request_id}-{attempt_id}.json"
    progress = {
        "protocol_version": DOWNLOAD_PROTOCOL, "request_id": request_id, "attempt_id": attempt_id,
        "start_date": start.isoformat(), "end_date": end.isoformat(),
        "started_at": now.isoformat(), "scope": "chart_only",
        "historical_pit_eligible": False, "coverage_certified": False,
        "universe_count": universe_count, "requested_count": len(codes), "requested_codes": codes,
        "universe_fetched_at": (universe or {}).get("fetched_at"),
        "limited": args.limit is not None, "state": "running", "stocks": {},
    }

    def publish():
        write_download_json(request_path, progress)
        write_download_json(progress_path, progress)

    publish()
    print(f"历史看图下载：{len(codes)}只，{start}~{end}；不写交易库，不证明历史完整覆盖", flush=True)
    try:
        for index, code in enumerate(codes, 1):
            previous = None
            if not args.refresh:
                try:
                    previous = load_kline_download(root, code)
                except ValueError:
                    pass  # Corrupt/incomplete local files are retried, never called complete.
            if (previous and previous["start_date"] == start.isoformat()
                    and previous["end_date"] == end.isoformat()
                    and not previous["coverage"].get("unavailable_years")):
                progress["stocks"][code] = {"status": "skipped_existing", "rows": len(previous["klines"])}
                publish()
                print(f"[{index}/{len(codes)}] {code} 已有同范围下载，跳过", flush=True)
                continue
            coverage = {}
            try:
                raw = await source.collect_init(code, coverage=coverage)
                records = [row for row in raw if start <= date.fromisoformat(row["trade_date"]) <= end]
                if not records:
                    progress["stocks"][code] = {"status": "empty", "rows": 0, "coverage": coverage}
                else:
                    # An unavailable year could be pre-listing OR a transport/source failure.
                    # Neither case is certified from this endpoint; keep both explicit.
                    coverage["unavailable_years"] = [
                        year for year in coverage.get("unavailable_years", [])
                        if start.year <= year <= end.year
                    ]
                    saved = save_kline_download(root, code, records, start=start, end=end,
                                                downloaded_at=download_now(), coverage=coverage)
                    progress["stocks"][code] = {
                        **saved, "status": "partial" if coverage["unavailable_years"] else "downloaded",
                    }
            except Exception as exc:
                # Error type only: upstream URLs/responses can contain credentials.
                progress["stocks"][code] = {"status": "failed", "rows": 0, "error_type": type(exc).__name__}
            publish()
            item = progress["stocks"][code]
            print(f"[{index}/{len(codes)}] {code} {item['status']} {item['rows']}条", flush=True)
            await asyncio.sleep(source.rate_limit)
    finally:
        states = [item["status"] for item in progress["stocks"].values()]
        progress["counts"] = {state: states.count(state) for state in (
            "downloaded", "skipped_existing", "partial", "empty", "failed",
        )}
        progress["unprocessed_count"] = len(codes) - len(states)
        progress["state"] = ("finished" if len(states) == len(codes) else "interrupted")
        progress["finished_at"] = download_now().isoformat()
        publish()
    print(json.dumps(progress["counts"], ensure_ascii=False), flush=True)
    print("缺失年份可能为上市前/源故障；看图下载不等于完整市场覆盖或模拟盘历史。", flush=True)
    return 2 if any(progress["counts"][key] for key in ("partial", "empty", "failed")) else 0


async def main() -> int:
    args = parse_args()
    if args.download:
        return await download_history(args)
    database = args.database.expanduser().resolve()
    if not database.is_file():
        raise SystemExit(f"数据库不存在: {database}")
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{database}"
    from app.data.kline_observations import persist_kline_observations
    from app.data.sources.ths_kline_source import ThsKlineSource
    from app.db.session import async_session, engine

    source = ThsKlineSource()
    records = []
    for code in dict.fromkeys(args.codes):
        klines = await source.collect_init(code)
        print(f"code={code} rows={len(klines)} "
              f"range={klines[0]['trade_date'] if klines else '-'}~{klines[-1]['trade_date'] if klines else '-'}")
        records.extend(klines)
    if not args.apply or not records:
        print("mode=dry-run" if not args.apply else "mode=apply no-op")
        return 0
    try:
        async with async_session() as session:
            result = await persist_kline_observations(session, records)
            await session.commit()
        print(f"mode=append-unreviewed observations={result['observations_appended']} "
              f"projection_writes={result['written']} historical_pit_eligible=False")
        return 0
    finally:
        await engine.dispose()


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("已中断；重新执行相同命令可继续，不删除已有下载。")
        raise SystemExit(130)
