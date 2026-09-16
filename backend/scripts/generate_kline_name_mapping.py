#!/usr/bin/env python3
"""生成pywencai→AkShare板块名称映射, 并更新SectorInfo.kline_name

问题: pywencai概念名与AkShare概念名约4%不匹配
解决: 对比两源名称, 匹配的kline_name=None(直接用sector_name), 不匹配的手动映射

行业: 100%匹配(90/90), 无需映射
概念: 95.9%匹配(376/392有K线), 2个映射 + 16个无K线源

用法:
  python3 scripts/generate_kline_name_mapping.py          # 生成映射+更新DB
  python3 scripts/generate_kline_name_mapping.py --dry-run # 只打印, 不写DB
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import akshare as ak
import pandas as pd
from loguru import logger
from sqlalchemy import select, and_, update

from app.db.session import async_session, init_db  # 2026-09-17: app.core.db 已不存在
from app.data.sources.wencai_stream_source import WencaiStreamSource
from app.models.stock import SectorInfo


# ===== 已知的手动映射(pywencai名 → AkShare名) =====
# 来源: 实测AkShare概念列表375个 + 同花顺APP逐一核对
# None = 名称一致, 不需要映射
# str = 需要映射到AkShare的名称
# False = AkShare API无此概念(但同花顺APP有), 无法获取K线
MANUAL_CONCEPT_MAP = {
    # ✅ 已确认映射(2026-04-13 用户核对)
    "CPO概念": "共封装光学(CPO)",        # CPO缩写+括号说明, 精确匹配
    "算力概念": "东数西算(算力)",         # 算力关键词匹配, 精确匹配

    # ❌ 同花顺APP有独立概念, 但AkShare API未收录(2026-04-13 用户确认)
    "AIGC概念": False,
    "C2M概念": False,
    "CRO概念": False,
    "ChatGPT概念": False,
    "DRG/DIP": False,
    "HJT电池": False,
    "MLOps概念": False,
    "MR(混合现实)": False,
    "MicroLED概念": False,
    "NMN概念": False,
    "PVDF概念": False,
    "Web3.0": False,
    "web3.0": False,                       # 与Web3.0重复
    "人民币贬值受益": False,
    "光伏建筑一体化": False,
    "跨境支付(CIPS)": False,
    
    # ✅ AkShare API实际有(2026-04-13 补验证+补采成功)
    # 之前误标为False, 实际名称精确匹配AkShare
    "2025年报预增": None,
    "2026一季报预增": None,
    "IP经济(谷子经济)": None,
    "DeepSeek概念": None,
    "中国AI 50": None,
    "兵装重组概念": None,
    "华为手机": None,
    "同花顺新质50": None,
    "小红书概念": None,
    "智谱AI": None,
    "雅下水电概念": None,
    "雄安新区": None,
    "数据安全": None,  # 之前漏采
}

# AkShare独有概念(不在pywencai中), 需注册到SectorInfo
AKSHARE_ONLY_CONCEPTS = {
    "举牌": "concept",
}


async def get_sector_info_map(session) -> dict:
    """获取DB中所有SectorInfo的 {sector_name: SectorInfo}"""
    result = await session.execute(
        select(SectorInfo).where(
            and_(
                SectorInfo.source == "pywencai",
                SectorInfo.sector_type.in_(["concept", "industry"]),
            )
        )
    )
    sectors = result.scalars().all()
    return {s.sector_name: s for s in sectors}


def build_name_mapping(ak_con_names: set, ak_ind_names: set,
                       pyw_con_names: set, pyw_ind_l2_names: set) -> dict:
    """构建名称映射: pywencai名 → AkShare名
    
    返回: {pywencai_name: akshare_name_or_None}
    - None表示直接用sector_name(名称一致)
    - str表示需要用此名称调AkShare
    - 明确不能映射的标记为False
    """
    mapping = {}
    
    # 概念: 先自动匹配, 再补手动映射
    con_matched = pyw_con_names & ak_con_names
    con_only_pyw = pyw_con_names - ak_con_names
    
    for name in con_matched:
        mapping[name] = None  # 直接用sector_name
    
    for name in con_only_pyw:
        if name in MANUAL_CONCEPT_MAP:
            mapping[name] = MANUAL_CONCEPT_MAP[name]
        else:
            # 尝试模糊匹配: 去掉"概念"后缀
            base = name.replace("概念", "").strip()
            if base in ak_con_names:
                mapping[name] = base
            else:
                mapping[name] = False  # 无法映射
                logger.warning(f"无法映射概念(未在MANUAL_CONCEPT_MAP中): {name} (base={base})")
    
    # 行业: 100%匹配, 不需要映射
    for name in pyw_ind_l2_names:
        mapping[name] = None
    
    return mapping


async def update_kline_names(mapping: dict, dry_run: bool = False):
    """更新SectorInfo.kline_name"""
    async with async_session() as session:
        sector_map = await get_sector_info_map(session)
        
        updated = 0
        skipped = 0
        unmapped = 0
        
        for pyw_name, ak_name in mapping.items():
            if pyw_name not in sector_map:
                # 可能是二级名而非完整三级名
                continue
            
            sector = sector_map[pyw_name]
            
            if ak_name is False:
                # AkShare无此概念, 标记为空字符串=无K线源
                if sector.kline_name != "":
                    if not dry_run:
                        sector.kline_name = ""
                    logger.info(f"  无K线源: {pyw_name} → 空字符串标记")
                    updated += 1
                unmapped += 1
                continue
            
            if ak_name is None:
                # 名称一致, 不需要kline_name
                if sector.kline_name is not None:
                    if not dry_run:
                        sector.kline_name = None
                    logger.info(f"  清除kline_name: {pyw_name} (名称一致)")
                    updated += 1
                else:
                    skipped += 1
                continue
            
            # 需要映射
            if sector.kline_name != ak_name:
                if not dry_run:
                    sector.kline_name = ak_name
                logger.info(f"  映射: {pyw_name} → {ak_name}")
                updated += 1
            else:
                skipped += 1
        
        if not dry_run and updated > 0:
            await session.commit()
        
        logger.info(f"映射更新: {updated}条修改, {skipped}条不变, {unmapped}条无法映射")
        
        # 补充AkShare独有概念
        for ak_name, sector_type in AKSHARE_ONLY_CONCEPTS.items():
            if ak_name not in sector_map:
                sector_code = f"ak_con_{hash(ak_name) % 100000:05d}"
                if not dry_run:
                    session.add(SectorInfo(
                        sector_code=sector_code,
                        sector_name=ak_name,
                        sector_type=sector_type,
                        source="akshare",
                        kline_name=None,  # 自己就是AkShare名
                    ))
                logger.info(f"  新增AkShare独有概念: {ak_name} ({sector_code})")
        
        if not dry_run:
            await session.commit()


async def main(dry_run: bool = False):
    await init_db()
    
    logger.info("=== 生成pywencai→AkShare名称映射 ===")
    
    # 1. 获取AkShare板块列表
    logger.info("获取AkShare板块列表...")
    ths_con = ak.stock_board_concept_name_ths()
    ak_con_names = set(ths_con['name'].tolist())
    logger.info(f"  AkShare概念: {len(ak_con_names)}个")
    
    ths_ind = ak.stock_board_industry_name_ths()
    ak_ind_names = set(ths_ind['name'].tolist())
    logger.info(f"  AkShare行业: {len(ak_ind_names)}个")
    
    # 2. 获取问财概念和行业
    # 2026-09-17：旧 `pywencai` 库自 8 月下旬起完全失效（上游改 SSE 流），
    # 改用 `WencaiStreamSource`。两处列名随之变化，均已按实测对齐：
    #   * `概念板块` 问句在新源下返回**概念指数清单**，概念名在 `指数简称`
    #     （旧列名 `所属概念` 不存在 —— 不改会静默得到空集）。
    #     实测 390 个指数简称与库内 pywencai 概念板块交集 388 个，口径一致。
    #   * `同花顺行业级别3` 在新源下只给三级名 `所属同花顺三级行业`
    #     （不含 `一级-二级-三级`），无法再 `split('-')[1]` 取二级。
    #     改为用标准映射问句的 `所属同花顺行业`（`A-B-C` 全称）取二级，
    #     与 `scheduler._premarket` / `collect_pywencai_sectors.py` 同口径。
    logger.info("获取问财概念(概念指数清单)...")
    pyw_con = WencaiStreamSource().query('概念板块', perpage=1000)
    con_names = {
        str(name).strip()
        for name in pyw_con.get('指数简称', pd.Series(dtype=object)).dropna()
        if str(name).strip()
    }
    logger.info(f"  问财概念: {len(con_names)}个")

    logger.info("获取问财行业(三级全称)...")
    pyw_ind = WencaiStreamSource().query(
        '全部A股 所属同花顺行业 所属概念', perpage=10000
    )
    ind_l2_names = {
        str(value).split('-')[1].strip()
        for value in pyw_ind.get('所属同花顺行业', pd.Series(dtype=object)).dropna()
        if '-' in str(value)
    }
    logger.info(f"  问财行业二级: {len(ind_l2_names)}个")
    
    # 3. 构建映射
    mapping = build_name_mapping(ak_con_names, ak_ind_names, con_names, ind_l2_names)
    
    # 统计
    auto_match = sum(1 for v in mapping.values() if v is None)
    manual_match = sum(1 for v in mapping.values() if isinstance(v, str))
    no_match = sum(1 for v in mapping.values() if v is False)
    logger.info(f"映射统计: 自动匹配={auto_match}, 手动映射={manual_match}, 无法映射={no_match}")
    
    # 4. 更新DB
    await update_kline_names(mapping, dry_run=dry_run)
    
    logger.info("=== 完成 ===")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="pywencai→AkShare板块名称映射")
    parser.add_argument("--dry-run", action="store_true", help="只打印, 不写DB")
    args = parser.parse_args()
    
    asyncio.run(main(dry_run=args.dry_run))
