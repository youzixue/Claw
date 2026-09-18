"""09:25 竞价证据采样后的判定脚本（只读）。

在 2026-09-18 09:25 首次实跑之后运行，一次给出四条**预先写死**的判定所需事实：

    python3 scripts/check_auction_evidence_after_0925.py [YYYY-MM-DD]

判定标准（在实跑前已固定，不得事后调整）
----------------------------------------
| 观察                                                    | 结论 |
| ------------------------------------------------------- | ---- |
| 腾讯与东财都 written>0，且每只 >=2 个不同 source_quote_at | 双来源成立，闸门分子条件满足 → 首板九路线可解封 |
| 只有一边 written>0                                       | 单来源，multi_frame_complete_count 仍为 0 → 闸门仍 blocked |
| 两边都 written=0 且出现 invalid_clock/unverified_basis    | 供应商时间戳未刷新到 09:25 —— 未知项被证伪，需换证据口径 |
| 出现「疑似被限频」                                        | 加固生效但确实限频 → 需降批量或改双轮采样 |

背景：`auction_data` 历史 156 万行 `source_quote_at` 100% 为 NULL，
`auction_evidence_status` 从未出现过 `ok`，导致 `active_confirmation`
恒为 False，进而**九条首板路线的 `actionable` 恒为 0**
（合计约 5.6 万条预测，actionable 总计 4 条）。
本脚本不修任何东西，只报事实。
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from collections import Counter
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DB = REPO / "backend" / "claw.db"


def sqlite_ro() -> sqlite3.Connection:
    return sqlite3.connect(f"file:{DB}?mode=ro", uri=True)


def report_auction_rows(c: sqlite3.Connection, day: str) -> None:
    print("=== 1. 今日 auction_data 写入（按来源）===")
    rows = c.execute(
        "SELECT source, source_version, COUNT(*), COUNT(DISTINCT code), "
        "       SUM(CASE WHEN source_quote_at IS NOT NULL THEN 1 ELSE 0 END) "
        "FROM auction_data WHERE trade_date=? GROUP BY source, source_version "
        "ORDER BY 3 DESC", (day,),
    ).fetchall()
    if not rows:
        print("  （今日尚无任何 auction_data 行）")
        return
    print(f"  {'source':<14}{'version':<32}{'行数':>7}{'代码数':>8}{'有报价时间':>10}")
    for src, ver, n, codes, with_clock in rows:
        print(f"  {str(src):<14}{str(ver):<32}{n:>7}{codes:>8}{with_clock or 0:>10}")


def report_frames(c: sqlite3.Connection, day: str) -> None:
    print("\n=== 2. 每只代码的「不同 source_quote_at 帧数」（闸门分子口径）===")
    rows = c.execute(
        "SELECT code, COUNT(DISTINCT source_quote_at) AS frames "
        "FROM auction_data WHERE trade_date=? "
        "  AND auction_time BETWEEN '09:15:00' AND '09:25:30' "
        "  AND source_quote_at IS NOT NULL "
        "GROUP BY code", (day,),
    ).fetchall()
    if not rows:
        print("  （无可用帧）")
        return
    dist = Counter(int(f) for _c, f in rows)
    print(f"  有帧代码数 = {len(rows)}")
    for frames in sorted(dist):
        print(f"    帧数={frames:<3} → {dist[frames]:>5} 只")
    ge2 = sum(v for k, v in dist.items() if k >= 2)
    print(f"  → 满足 >=2 帧（multi_frame_complete_count）= {ge2}")


def report_evidence_status() -> None:
    print("\n=== 3. 契约判定（复用生产的 get_snapshot_health，不重算）===")
    try:
        import sys as _sys

        _sys.path.insert(0, str(REPO / "backend"))
        from app.db.session import async_session
        from app.strategy.auction import auction_collector

        async def _run(day: date):
            async with async_session() as session:
                return await auction_collector.get_snapshot_health(session, day)

        day = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else date.today()
        health = asyncio.run(_run(day))
    except Exception as exc:                      # noqa: BLE001
        print(f"  （调用失败：{type(exc).__name__}: {exc}）")
        return
    for key in (
        "snapshot_count", "latest_snapshot_time", "evidence_status_counts",
        "feed_complete_count", "feed_complete_ratio",
        "multi_frame_complete_count", "multi_frame_complete_ratio",
        "timely_snapshot_ratio", "verified_timely_snapshot_ratio",
        "tradeable_universe_count", "path_degraded", "stale", "missing",
    ):
        if key in health:
            print(f"  {key:<32} = {json.dumps(health[key], ensure_ascii=False)}")


def report_rate_limit(c: sqlite3.Connection, day: str) -> None:
    print("\n=== 4. 限频留痕（DataSourceHealth）===")
    rows = c.execute(
        "SELECT source, api_name, status, fail_streak, last_failure, error_msg "
        "FROM data_source_health WHERE api_name='auction_quote_rate_limit' "
        "ORDER BY id DESC LIMIT 5"
    ).fetchall()
    if not rows:
        print("  未记录到限频（说明两个主机都没触发 429/5xx/错误页）")
        return
    for src, api, status, streak, when, msg in rows:
        print(f"  {src}/{api} status={status} streak={streak} at={when}")
        print(f"      {str(msg)[:160]}")


def report_actionable_attribution(c: sqlite3.Connection, day: str) -> None:
    print("\n=== 5. 今日首板候选「为什么不可执行」归因 ===")
    rows = c.execute(
        "SELECT factors_json FROM promotion_prediction_record "
        "WHERE prediction_trade_date=?", (day,),
    ).fetchall()
    if not rows:
        print("  （今日尚无预测记录）")
        return
    counter: Counter = Counter()
    n = 0
    for (fj,) in rows:
        try:
            f = json.loads(fj or "{}")
        except Exception:                          # noqa: BLE001
            continue
        if "prediction_not_actionable_reasons" not in f:
            continue                                # 旧协议记录，无归因字段
        n += 1
        reasons = f.get("prediction_not_actionable_reasons") or []
        if not reasons:
            counter["(可执行)"] += 1
        for reason in reasons:
            counter[str(reason).split(":")[0]] += 1
    if not n:
        print("  （今日记录还没有归因字段；需 09:25 起的快照才带）")
        return
    print(f"  带归因字段的记录 = {n}")
    for reason, cnt in counter.most_common(12):
        print(f"    {reason:<52} {cnt}")


def main() -> int:
    day = sys.argv[1] if len(sys.argv) > 1 else date.today().isoformat()
    print(f"竞价证据实跑判定　trade_date = {day}\n")
    c = sqlite_ro()
    try:
        report_auction_rows(c, day)
        report_frames(c, day)
        report_evidence_status()
        report_rate_limit(c, day)
        report_actionable_attribution(c, day)
    finally:
        c.close()
    print("\n提示：判定标准见本文件 docstring；本脚本只读，不改任何状态。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
