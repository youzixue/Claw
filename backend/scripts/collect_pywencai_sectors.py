"""从pywencai采集全量行业/概念板块列表写入SectorInfo

口径: 257个三级行业 + 389个概念(从个股映射去重)
用法: python3 scripts/collect_pywencai_sectors.py
"""

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pywencai
import pandas as pd
from sqlalchemy import select, func
from app.db.session import async_session, init_db
from app.models.stock import SectorInfo

# 无业务关联的概念板块, 采集时自动标记排除(不展示在板块营地)
EXCLUDED_CONCEPTS = {
    "融资融券", "沪股通", "深股通", "证金持股", "国家大基金持股",
    "ST板块", "新股与次新股", "注册制次新股", "科创次新股",
    "回购增持再贷款概念", "同花顺果指数", "同花顺漂亮100", "同花顺中特估100",
    "同花顺出海50", "同花顺新质50", "中国AI 50",
    "摘帽", "股权转让(并购重组)", "兵装重组概念", "高股息精选", "超级品牌",
    "专精特新", "独角兽概念", "央企国企改革", "国企改革",
    "上海国企改革", "深圳国企改革", "中字头股票", "中芯国际概念", "中船系",
    "参股保险", "参股券商", "参股银行", "信托概念", "期货概念", "PPP概念",
}
# 年报/季报预增类板块(年更, 名称随年份变化) — 模式匹配
EXCLUDED_PATTERNS = ["预增", "预减", "预盈", "预亏"]


async def collect():
    await init_db()
    
    # ===== 1. pywencai全量采集 =====
    print("=== pywencai全量采集(约45秒) ===")
    t0 = time.time()
    df = pywencai.get(
        query="全部A股 所属同花顺行业 所属概念",
        query_type="stock",
        loop=True,  # 关键: 自动分页获取全量
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
            is_exc = con_name in EXCLUDED_CONCEPTS or any(p in con_name for p in EXCLUDED_PATTERNS)
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
