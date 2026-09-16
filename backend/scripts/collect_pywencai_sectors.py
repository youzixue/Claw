"""从问财采集全量行业/概念板块列表写入SectorInfo

口径: 257个三级行业 + 389个概念(从个股映射去重)
用法: python3 scripts/collect_pywencai_sectors.py
"""

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.data.sources.wencai_stream_source import WencaiStreamSource
import pandas as pd
from sqlalchemy import select, func
from app.db.session import async_session, init_db
from app.models.stock import SectorInfo
# 排除规则已上移到 app 层，与调度器盘前写入路径共用同一份口径。
# 2026-09-17 起概念板块由调度器写入（本脚本不再经过），若各留一份会漂移。
from app.data.sector_exclusions import is_excluded_concept


async def collect():
    await init_db()
    
    # ===== 1. 问财全量采集 =====
    # 2026-09-17：旧 `pywencai` 库自 8 月下旬起完全失效（上游改 SSE 流），
    # 改用 `WencaiStreamSource`（同一问句）。`perpage` 必须给足 —— 默认 50
    # 只会拿到首页 N 条，全 A 股约 5574 只。
    print("=== 问财全量采集(约10-20秒) ===")
    t0 = time.time()
    df = WencaiStreamSource().query(
        "全部A股 所属同花顺行业 所属概念",
        perpage=10000,
    )
    elapsed = time.time() - t0
    print(f"采集完成: {len(df)}条个股, 耗时{elapsed:.1f}秒")

    # ===== 2. 提取行业(三级257个) =====
    industry_col = None
    for col in df.columns:
        if "所属同花顺行业" in col and "一级" not in col:
            industry_col = col
            break
    
    if not industry_col:
        print("ERROR: 找不到'所属同花顺行业'列")
        return
    
    industries = df[industry_col].dropna().unique()
    print(f"\n行业板块(三级): {len(industries)}个")

    # ===== 3. 提取概念(389个) =====
    concept_col = None
    for col in df.columns:
        if "所属概念" in col and "数量" not in col:
            concept_col = col
            break
    
    if not concept_col:
        print("ERROR: 找不到'所属概念'列")
        return
    
    concepts = set()
    concept_stock_count = {}  # 概念→成分股数
    for val in df[concept_col].dropna():
        for c in str(val).split(";"):
            c = c.strip()
            if c:
                concepts.add(c)
                concept_stock_count[c] = concept_stock_count.get(c, 0) + 1
    print(f"概念板块: {len(concepts)}个")

    # ===== 4. 计算行业成分股数 =====
    industry_stock_count = {}
    for ind in df[industry_col].dropna():
        ind = str(ind).strip()
        industry_stock_count[ind] = industry_stock_count.get(ind, 0) + 1

    # ===== 5. 写入SectorInfo =====
    async with async_session() as session:
        new_industry = 0
        skip_industry = 0
        for ind_name in industries:
            ind_name = str(ind_name).strip()
            sector_code = f"pw_industry_{ind_name}"
            existing = await session.execute(
                select(SectorInfo).where(SectorInfo.sector_code == sector_code)
            )
            row = existing.scalar_one_or_none()
            if row:
                # 更新stock_count
                row.stock_count = industry_stock_count.get(ind_name, 0)
                skip_industry += 1
            else:
                session.add(SectorInfo(
                    sector_code=sector_code,
                    sector_name=ind_name,
                    sector_type="industry",
                    source="pywencai",
                    stock_count=industry_stock_count.get(ind_name, 0),
                ))
                new_industry += 1
        
        new_concept = 0
        skip_concept = 0
        excluded_concept = 0
        for con_name in concepts:
            con_name = str(con_name).strip()
            sector_code = f"pw_concept_{con_name}"
            # 检查是否在排除名单(无业务关联板块)
            is_exc = is_excluded_concept(con_name)
            existing = await session.execute(
                select(SectorInfo).where(SectorInfo.sector_code == sector_code)
            )
            row = existing.scalar_one_or_none()
            if row:
                row.stock_count = concept_stock_count.get(con_name, 0)
                row.is_excluded = 1 if is_exc else 0  # 同步排除标记
                skip_concept += 1
            else:
                session.add(SectorInfo(
                    sector_code=sector_code,
                    sector_name=con_name,
                    sector_type="concept",
                    source="pywencai",
                    stock_count=concept_stock_count.get(con_name, 0),
                    is_excluded=1 if is_exc else 0,
                ))
                new_concept += 1
                if is_exc:
                    excluded_concept += 1
        
        await session.commit()
        
        print(f"\n=== 写入结果 ===")
        print(f"行业板块: 新增{new_industry}个, 更新{skip_industry}个")
        print(f"概念板块: 新增{new_concept}个(排除{excluded_concept}个), 更新{skip_concept}个")
        
        # 验证总数
        result = await session.execute(
            select(SectorInfo.sector_type, func.count())
            .where(SectorInfo.source == "pywencai")
            .group_by(SectorInfo.sector_type)
        )
        print(f"\npywencai口径SectorInfo统计:")
        for row in result.all():
            print(f"  {row[0]}: {row[1]}个")

    print("\n✅ 全量板块采集完成!")


if __name__ == "__main__":
    asyncio.run(collect())
