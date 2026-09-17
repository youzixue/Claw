"""申万个股行业映射刷新（2026-09-17 复盘 T3-14 数据真值修复）。

用途
----
`stock_sector_mapping` 中 `source='shenwan'` 的行长期没有 `observed_at`，
根因是 `data_source_health` 里 `shenwan/stock_mapping` 一直 down：
申万站点只下发叶子证书、不下发中间证书，导致
`SSLCertVerificationError: unable to get local issuer certificate`，
`fail_streak=51`、`last_success=None`。证书链已由 `app/core/tls_trust.py`
补齐（**仍执行完整链验证**）。

本脚本复刻 `DataScheduler._deep_review` 中"申万个股行业映射"这一段
（同一数据源方法 `get_stock_industry_clf`、同一 `_upsert_stock_sector_mapping`
助手、同一 sector_type 推断），用于在不触发 `_deep_review` 其余副作用的前提下
把 `observed_at` 补写进该表。**幂等**：冲突键为 (code, sector_code, source)，
重复执行只会刷新 `observed_at`/`sector_name`。

用法
----
    cd backend && python ../scripts/refresh_shenwan_sector_mapping.py
"""
import asyncio
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from sqlalchemy import select

from app.core.tls_trust import install_extra_ca_bundle

install_extra_ca_bundle()          # 必须在任何网络调用之前

from app.data.scheduler import DataScheduler, _normalize_code
from app.db.session import async_session
from app.models.stock import SectorInfo


async def main() -> int:
    scheduler = DataScheduler.__new__(DataScheduler)   # 只用静态助手，不启动调度器
    async with async_session() as session:
        sw = scheduler._sources["shenwan"] if hasattr(scheduler, "_sources") else None
        if sw is None:
            from app.data.sources.sw_source import ShenwanSource
            sw = ShenwanSource()

        df = await sw.get_stock_industry_clf()
        if df is None or len(df) == 0:
            print("❌ 申万返回空，未写入")
            return 1
        print(f"申万返回 {len(df)} 行")

        latest = df.sort_values("update_time", ascending=False).drop_duplicates(
            subset=["symbol"], keep="first")
        print(f"按 symbol 去重取最新后 {len(latest)} 只")

        rows = (await session.execute(
            select(SectorInfo).where(SectorInfo.source == "shenwan"))).scalars().all()
        sector_map = {s.sector_code: s.sector_name for s in rows}

        from datetime import datetime
        observed_at = datetime.now()
        records = []
        for _, row in latest.iterrows():
            code = _normalize_code(str(row.get("symbol", "")))
            industry_code = str(row.get("industry_code", ""))
            if not code or not industry_code:
                continue
            if industry_code.endswith("01") and len(industry_code) == 6:
                sector_type = "sw_l1"
            elif len(industry_code) == 6:
                sector_type = "sw_l2"
            elif len(industry_code) == 8:
                sector_type = "sw_l3"
            else:
                sector_type = "sw_l1"
            records.append({
                "code": code,
                "sector_code": industry_code,
                "sector_name": sector_map.get(industry_code, ""),
                "sector_type": sector_type,
                "source": "shenwan",
                "source_version": "industry_clf_latest_v1",
                "observed_at": observed_at,
                "weight": None,
            })
        print(f"待写入 {len(records)} 条，sector_type 分布 {dict(Counter(r['sector_type'] for r in records))}")

        for i in range(0, len(records), 100):
            await DataScheduler._upsert_stock_sector_mapping(session, records[i:i + 100])
        await session.commit()
        print(f"✅ 已提交 {len(records)} 条，observed_at={observed_at}")
    return 0


raise SystemExit(asyncio.run(main()))
