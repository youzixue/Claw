"""采集调度器 — 统一管理所有数据源的定时采集+写入DB

每个采集方法现在完整实现：采集 → 字段映射 → 质量校验 → DB写入(upsert) → 质量记录
"""

import asyncio
import time as _time
from datetime import datetime, date
from typing import Optional

import pandas as pd
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.core.trade_calendar import trade_calendar
from app.core.data_quality import data_quality_guard
from app.core.stock_tagger import stock_tagger
from app.data.sources.akshare_source import AkShareSource
from app.data.sources.eastmoney_source import EastMoneySource
from app.data.sources.pywencai_source import PyWencaiSource
from app.data.sources.sw_source import ShenwanSource
from app.data.sources.sina_source import SinaSource
from app.data.sources.index_source import IndexSource
from app.db.session import async_session

# ORM Models
from app.models.stock import (
    SectorInfo, StockSectorMapping, BoardCons,
    LimitUpPool, LimitDownPool, BrokenLimitPool,
    FundFlow, MarketSentiment, SectorPersistence,
    StockTag, StockBlacklist, StockDaily, AuctionData, MarginData,
)


class DataScheduler:
    """数据采集调度器 — 采集+写入一体化"""

    def __init__(self):
        self.scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")
        self._sources = {
            "akshare": AkShareSource(),
            "eastmoney": EastMoneySource(),
            "pywencai": PyWencaiSource(),
            "shenwan": ShenwanSource(),
            "sina": SinaSource(),
            "index": IndexSource(),
        }

    def setup_jobs(self):
        """设置定时任务 — 优化版(v2.0: 秒级延时)"""

        # === 盘前 (8:25) 股票状态更新 ===
        self.scheduler.add_job(
            self._update_stock_status, CronTrigger(hour=8, minute=25, day_of_week="mon-fri"),
            id="update_stock_status", name="股票状态更新(ST/停牌/退市)",
        )

        # === 盘前 (8:30) 板块列表+申万 ===
        self.scheduler.add_job(
            self._pre_market, CronTrigger(hour=8, minute=30, day_of_week="mon-fri"),
            id="pre_market", name="盘前数据准备",
        )

        # === 竞价采集 9:15-9:25 每30秒 ===
        self.scheduler.add_job(
            self._auction_collect, IntervalTrigger(seconds=30),
            id="auction_collect", name="竞价数据采集",
        )

        # === 盘中快频 — 每10秒(涨停/跌停/炸板) ===
        self.scheduler.add_job(
            self._intraday_fast, IntervalTrigger(seconds=10),
            id="intraday_fast", name="盘中快频采集(10s)",
        )

        # === 盘中慢频 — 每30秒(资金流) ===
        self.scheduler.add_job(
            self._intraday_slow, IntervalTrigger(seconds=30),
            id="intraday_slow", name="盘中慢频采集(30s)",
        )

        # === 指数快照 — 每60秒(三大指数+情绪) ===
        self.scheduler.add_job(
            self._intraday_indices_and_sentiment, IntervalTrigger(seconds=60),
            id="intraday_indices_and_sentiment", name="指数与情绪快照(60s)",
        )

        # === 盘中新浪 — 每5分钟(概念板块行情) ===
        self.scheduler.add_job(
            self._intraday_sina, IntervalTrigger(minutes=5),
            id="intraday_sina", name="盘中新浪概念行情",
        )

        # === 盘后 (15:05) ===
        self.scheduler.add_job(
            self._after_market, CronTrigger(hour=15, minute=5, day_of_week="mon-fri"),
            id="after_market", name="盘后数据补全",
        )

        # === 深度复盘 (20:00) ===
        self.scheduler.add_job(
            self._deep_review, CronTrigger(hour=20, minute=0, day_of_week="mon-fri"),
            id="deep_review", name="盘后深度数据",
        )

        # === 派生计算: 板块持续性+强弱+生命周期 每2分钟 ===
        self.scheduler.add_job(
            self._sector_derive, IntervalTrigger(minutes=2),
            id="sector_derive", name="板块派生计算(持续性+强弱+生命周期)",
        )

        # === overview-v2 快照: 每60秒 ===
        self.scheduler.add_job(
            self._dashboard2_snapshot, IntervalTrigger(seconds=60),
            id="dashboard2_snapshot", name="overview-v2 快照刷新(60s)",
        )

        # === 数据质量检查: 每5分钟 ===
        self.scheduler.add_job(
            self._quality_check, IntervalTrigger(minutes=5),
            id="quality_check", name="数据质量检查",
        )

        logger.info("采集调度器任务设置完成(v2.0: 快频10s/慢频30s/竞价30s/新浪5min/派生2min)")

    # =========================================================================
    # 通用写入辅助
    # =========================================================================

    @staticmethod
    async def _batch_upsert(session: AsyncSession, table_class, records: list[dict],
                             unique_cols: list[str], update_cols: list[str] = None):
        """批量 Upsert — INSERT ON CONFLICT DO UPDATE (单条SQL，极速)

        Args:
            table_class: ORM类
            records: 记录列表
            unique_cols: 冲突检测列(ON CONFLICT)
            update_cols: 冲突时更新列(空=更新全部非主键列)

        性能: 5190条FundFlow: 逐条upsert≈5s, 批量INSERT≈50ms (100x提速)
        """
        if not records:
            return

        from sqlalchemy import text as sa_text

        table = table_class.__table__
        table_name = table.name
        columns = [c.name for c in table.columns if c.name != "id"]

        # 构建列名列表
        col_str = ", ".join(columns)
        # 参数占位符(每条记录一组)
        param_str = ", ".join([f":{c}" for c in columns])
        # ON CONFLICT 列
        conflict_str = ", ".join(unique_cols)
        # DO UPDATE SET 列
        if update_cols is None:
            update_cols = [c for c in columns if c not in unique_cols]
        update_str = ", ".join([f"{c} = EXCLUDED.{c}" for c in update_cols])

        sql = (
            f"INSERT INTO {table_name} ({col_str}) "
            f"VALUES ({param_str}) "
            f"ON CONFLICT ({conflict_str}) DO UPDATE SET {update_str}"
        )

        # 只保留columns中存在的字段，清理数据
        clean_records = []
        for r in records:
            clean = {}
            for c in columns:
                if c in r:
                    val = r[c]
                    # 日期转字符串(SQLite兼容)
                    if isinstance(val, date):
                        val = val.isoformat()
                    clean[c] = val
            clean_records.append(clean)

        # 逐条执行(SQLite不支持VALUES多行，但aiosqlite的execute可批量传参)
        stmt = sa_text(sql)
        for clean in clean_records:
            await session.execute(stmt, clean)

    @staticmethod
    async def _upsert_sector_info(session: AsyncSession, records: list[dict]):
        """Upsert 板块信息 → SectorInfo 表

        records 字段要求:
          sector_code: str  (唯一键)
          sector_name: str
          sector_type: str  (industry/concept/sw_l1/sw_l2/sw_l3)
          source: str       (akshare/eastmoney/sw/sina)
          parent_code: Optional[str]
          stock_count: Optional[int]
          avg_pe: Optional[float]
          avg_pb: Optional[float]
          avg_dividend: Optional[float]
        """
        for r in records:
            existing = await session.execute(
                select(SectorInfo).where(SectorInfo.sector_code == r["sector_code"])
            )
            row = existing.scalar_one_or_none()
            if row:
                for k, v in r.items():
                    setattr(row, k, v)
            else:
                session.add(SectorInfo(**r))
        await session.flush()

    @staticmethod
    async def _upsert_stock_sector_mapping(session: AsyncSession, records: list[dict]):
        """Upsert 个股→板块映射 → StockSectorMapping 表

        records 字段要求:
          code: str
          sector_code: str
          sector_name: Optional[str]
          sector_type: Optional[str]
          source: str
          weight: Optional[float]
        """
        for r in records:
            existing = await session.execute(
                select(StockSectorMapping).where(
                    StockSectorMapping.code == r["code"],
                    StockSectorMapping.sector_code == r["sector_code"],
                    StockSectorMapping.source == r["source"],
                )
            )
            row = existing.scalar_one_or_none()
            if row:
                for k, v in r.items():
                    setattr(row, k, v)
            else:
                session.add(StockSectorMapping(**r))
        await session.flush()

    @staticmethod
    async def _upsert_limit_up(session: AsyncSession, records: list[dict]):
        """Upsert 涨停池 → LimitUpPool 表

        records 字段要求:
          code: str, name: Optional[str], trade_date: date
          limit_up_time: Optional[str], limit_up_price: Optional[float]
          seal_amount: Optional[float], break_count: Optional[int]
          consecutive_days: Optional[int], limit_up_reason: Optional[str]
          turnover: Optional[float], source: str
        """
        for r in records:
            existing = await session.execute(
                select(LimitUpPool).where(
                    LimitUpPool.code == r["code"],
                    LimitUpPool.trade_date == r["trade_date"],
                )
            )
            row = existing.scalar_one_or_none()
            if row:
                for k, v in r.items():
                    setattr(row, k, v)
            else:
                session.add(LimitUpPool(**r))
        await session.flush()

    @staticmethod
    async def _upsert_limit_down(session: AsyncSession, records: list[dict]):
        """Upsert 跌停池 → LimitDownPool 表"""
        for r in records:
            existing = await session.execute(
                select(LimitDownPool).where(
                    LimitDownPool.code == r["code"],
                    LimitDownPool.trade_date == r["trade_date"],
                )
            )
            row = existing.scalar_one_or_none()
            if row:
                for k, v in r.items():
                    setattr(row, k, v)
            else:
                session.add(LimitDownPool(**r))
        await session.flush()

    @staticmethod
    async def _upsert_broken_limit(session: AsyncSession, records: list[dict]):
        """Upsert 炸板池 → BrokenLimitPool 表"""
        for r in records:
            existing = await session.execute(
                select(BrokenLimitPool).where(
                    BrokenLimitPool.code == r["code"],
                    BrokenLimitPool.trade_date == r["trade_date"],
                )
            )
            row = existing.scalar_one_or_none()
            if row:
                for k, v in r.items():
                    setattr(row, k, v)
            else:
                session.add(BrokenLimitPool(**r))
        await session.flush()

    @staticmethod
    async def _upsert_fund_flow(session: AsyncSession, records: list[dict]):
        """Upsert 资金流 → FundFlow 表

        records 字段要求:
          code: str, name: Optional[str], trade_date: date
          main_net_inflow: Optional[float], main_net_inflow_pct: Optional[float]
          big_net_inflow: Optional[float], mid_net_inflow: Optional[float]
          small_net_inflow: Optional[float]
        """
        for r in records:
            existing = await session.execute(
                select(FundFlow).where(
                    FundFlow.code == r["code"],
                    FundFlow.trade_date == r["trade_date"],
                )
            )
            row = existing.scalar_one_or_none()
            if row:
                for k, v in r.items():
                    setattr(row, k, v)
            else:
                session.add(FundFlow(**r))
        await session.flush()

    # =========================================================================
    # 采集方法
    # =========================================================================

    async def _update_stock_status(self):
        """更新股票状态(ST/停牌/退市) → StockTag + StockBlacklist

        数据源: pywencai 问财(最全最准，含北交所)
        频率: 盘前8:25 + 盘后自动调用
        优化: pywencai.get(loop=True) 4查询并行 + 批量写入
        性能: ~2s (采集≈2s + 解析≈5ms + 写入≈20ms)
        """
        if not await trade_calendar.is_trade_day():
            return

        t_total = _time.monotonic()
        loop = asyncio.get_event_loop()
        today = date.today()

        # ==== 阶段1: 4查询并行采集 (pywencai.get loop=True自动翻页) ====
        t_fetch = _time.monotonic()
        import pywencai as _pw
        try:
            df_st, df_sus, df_del, df_bse_st = await asyncio.gather(
                loop.run_in_executor(
                    None, lambda: _pw.get(query="ST股", loop=True)),
                loop.run_in_executor(
                    None, lambda: _pw.get(query="停牌", loop=True)),
                loop.run_in_executor(
                    None, lambda: _pw.get(query="*ST股", loop=True)),
                loop.run_in_executor(
                    None, lambda: _pw.get(query="北交所ST股", loop=True)),
            )
        except Exception as e:
            logger.error(f"股票状态采集失败: {e}")
            return

        fetch_ms = int((_time.monotonic() - t_fetch) * 1000)

        # ==== 阶段2: 解析数据 → {code: name} 映射 ====
        def _parse_df(df, code_col="股票代码", name_col="股票简称"):
            result = {}
            if df is None or len(df) == 0:
                return result
            for _, row in df.iterrows():
                code = _normalize_code(str(row.get(code_col, "")))
                name = str(row.get(name_col, ""))
                if code:
                    result[code] = name
            return result

        st_map = _parse_df(df_st)
        suspended_map = _parse_df(df_sus)
        delisting_map = _parse_df(df_del)
        bse_st_map = _parse_df(df_bse_st)
        # 合并北交所ST到主ST映射
        st_map.update(bse_st_map)

        # ==== 阶段3: 批量写入DB ====
        all_codes = set(st_map) | set(suspended_map) | set(delisting_map)
        restored_count = 0
        untagged_count = 0
        undel_count = 0

        async with async_session() as session:
            try:
                # 3a. 增量清理: 复牌/ST摘帽/退市摘帽
                #   - 复牌: 之前suspended但今日不在停牌列表
                #   - ST摘帽: 之前ST但今日不在ST列表
                #   - 退市摘帽: 之前delisting但今日不在退市列表
                old_tags = (await session.execute(
                    select(StockTag)
                )).scalars().all()
                old_codes_to_delete = set()

                for tag in old_tags:
                    changed = False
                    # 复牌清理
                    if tag.is_suspended and tag.code not in suspended_map:
                        tag.is_suspended = False
                        if not tag.is_st and not tag.is_delisting:
                            tag.board_tag = stock_tagger.get_board_tag(
                                stock_tagger.get_board_type(tag.code))
                        changed = True
                        restored_count += 1
                    # ST摘帽
                    if tag.is_st and tag.code not in st_map:
                        tag.is_st = False
                        if tag.board_tag == "blocked" and tag.code not in delisting_map:
                            tag.board_tag = stock_tagger.get_board_tag(
                                stock_tagger.get_board_type(tag.code))
                        changed = True
                        untagged_count += 1
                    # 退市摘帽
                    if tag.is_delisting and tag.code not in delisting_map:
                        tag.is_delisting = False
                        if tag.board_tag == "blocked" and tag.code not in st_map:
                            tag.board_tag = stock_tagger.get_board_tag(
                                stock_tagger.get_board_type(tag.code))
                        changed = True
                        undel_count += 1
                    # 已完全恢复正常的 → 删除记录(不占空间)
                    if not tag.is_st and not tag.is_suspended and not tag.is_delisting:
                        old_codes_to_delete.add(tag.code)

                    if changed:
                        tag.updated_at = datetime.now()

                # 清理Blacklist中已恢复的记录
                if old_codes_to_delete:
                    for code in old_codes_to_delete:
                        bl = await session.get(StockBlacklist, code)
                        if bl and bl.end_date is None:
                            bl.end_date = today
                    # 删除已恢复正常的StockTag
                    for tag in old_tags:
                        if tag.code in old_codes_to_delete:
                            await session.delete(tag)

                # 3b. 批量写入: DELETE旧 + INSERT新
                if all_codes:
                    # 构建StockTag + Blacklist
                    tags = []
                    bl_entries = []
                    for code in all_codes:
                        name = (
                            st_map.get(code)
                            or suspended_map.get(code)
                            or delisting_map.get(code, "")
                        )
                        is_st = code in st_map
                        is_suspended = code in suspended_map
                        is_delisting = code in delisting_map

                        if is_suspended:
                            board_tag = "suspended"
                        elif is_st or is_delisting:
                            board_tag = "blocked"
                        else:
                            board_tag = stock_tagger.get_board_tag(
                                stock_tagger.get_board_type(code))

                        tags.append(StockTag(
                            code=code, name=name,
                            board_type=stock_tagger.get_board_type(code),
                            board_tag=board_tag,
                            is_st=is_st,
                            is_suspended=is_suspended,
                            is_delisting=is_delisting,
                        ))
                        # Blacklist优先级: delisting > suspended > st
                        reason = (
                            "delisting" if is_delisting
                            else "suspended" if is_suspended
                            else "st"
                        )
                        bl_entries.append(StockBlacklist(
                            code=code, reason=reason,
                            start_date=today, end_date=None,
                            auto_expire=True, source="auto",
                        ))

                    # 批量删除旧记录(比逐行session.get快100x)
                    code_list = list(all_codes)
                    placeholders = ",".join([f":c{i}" for i in range(len(code_list))])
                    params = {f"c{i}": c for i, c in enumerate(code_list)}
                    await session.execute(
                        text(f"DELETE FROM stock_tags WHERE code IN ({placeholders})"),
                        params,
                    )
                    await session.execute(
                        text(f"DELETE FROM stock_blacklist WHERE code IN ({placeholders})"),
                        params,
                    )
                    session.add_all(tags)
                    session.add_all(bl_entries)

                await session.commit()

                # 质量记录
                st_count = len(st_map)
                sus_count = len(suspended_map)
                del_count = len(delisting_map)
                bse_st_count = len(bse_st_map)
                await data_quality_guard.record_success(
                    session, "pywencai", "stock_status",
                    latency_ms=fetch_ms,
                    record_count=st_count + sus_count + del_count + bse_st_count,
                )

                total_ms = int((_time.monotonic() - t_total) * 1000)
                logger.info(
                    f"股票状态更新完成(总{total_ms}ms, 采集{fetch_ms}ms): "
                    f"ST={st_count}(含北交所{bse_st_count}), "
                    f"停牌={sus_count}, 退市风险={del_count}, "
                    f"复牌恢复={restored_count}, ST摘帽={untagged_count}, "
                    f"退市摘帽={undel_count}"
                )

            except Exception as e:
                logger.error(f"股票状态写入失败: {e}")
                await session.rollback()
                await data_quality_guard.record_failure(
                    session, "pywencai", "stock_status", str(e),
                )

    @staticmethod
    async def _upsert_blacklist(session: AsyncSession, code: str, reason: str,
                                 start_date: date, source: str = "auto"):
        """Upsert 股票黑名单 → StockBlacklist 表

        reason: st / delisting / suspended / ipo_recent
        """
        bl = await session.get(StockBlacklist, code)
        if bl:
            # 已存在且reason相同 → 更新start_date
            if bl.reason == reason:
                bl.start_date = start_date
                bl.end_date = None  # 重置结束日期
                bl.source = source
            else:
                # reason不同 → 更新为新reason
                bl.reason = reason
                bl.start_date = start_date
                bl.end_date = None
                bl.source = source
        else:
            bl = StockBlacklist(
                code=code, reason=reason,
                start_date=start_date, end_date=None,
                auto_expire=True, source=source,
            )
            session.add(bl)
        await session.commit()

    async def _pre_market(self):
        """盘前准备 — pywencai板块列表→SectorInfo + akshare板块列表补充 + 申万行业→SectorInfo
        
        板块口径(统一用pywencai):
        - 行业板块: pywencai全量5197只个股→去重257个三级行业/31个一级行业
        - 概念板块: pywencai全量5197只个股→去重389个概念
        - akshare/申万/新浪作为补充源, 不在板块营地页面展示申万
        """
        if not await trade_calendar.is_trade_day():
            return
        logger.info("📊 盘前数据准备开始...")
        t0 = _time.monotonic()
        async with async_session() as session:
            pw = self._sources["pywencai"]
            ak_src = self._sources["akshare"]
            sw = self._sources["shenwan"]

            # ---- 1. pywencai 个股映射→提取行业+概念板块列表 → SectorInfo ----
            try:
                t1 = _time.monotonic()
                df_mapping = await pw.get_stock_industry_mapping()
                if df_mapping is not None and len(df_mapping) > 0:
                    # 1a. 提取行业板块(三级全名"医药生物-中药-中药Ⅲ", 与collect_pywencai_sectors.py一致)
                    industries = set()
                    industry_counts = {}  # 行业→成分股数
                    for _, row in df_mapping.iterrows():
                        ind = str(row.get("所属同花顺行业", ""))
                        if ind and ind != "nan":
                            ind = ind.strip()
                            industries.add(ind)
                            industry_counts[ind] = industry_counts.get(ind, 0) + 1
                    
                    records = []
                    for ind_name in sorted(industries):
                        records.append({
                            "sector_code": f"pw_industry_{ind_name}",
                            "sector_name": ind_name,
                            "sector_type": "industry",
                            "source": "pywencai",
                            "stock_count": industry_counts.get(ind_name, 0),
                        })
                    await self._upsert_sector_info(session, records)
                    logger.info(f"pywencai行业板块写入: {len(records)}个(一级)")
                    
                    # 1b. 提取概念板块(分号分隔)
                    concepts = set()
                    concept_counts = {}
                    for _, row in df_mapping.iterrows():
                        con_str = str(row.get("所属概念", ""))
                        if con_str and con_str != "nan":
                            for concept in con_str.split(";"):
                                concept = concept.strip()
                                if concept:
                                    concepts.add(concept)
                                    concept_counts[concept] = concept_counts.get(concept, 0) + 1
                    
                    records = []
                    for con_name in sorted(concepts):
                        records.append({
                            "sector_code": f"pw_concept_{con_name}",
                            "sector_name": con_name,
                            "sector_type": "concept",
                            "source": "pywencai",
                            "stock_count": concept_counts.get(con_name, 0),
                        })
                    await self._upsert_sector_info(session, records)
                    logger.info(f"pywencai概念板块写入: {len(records)}个")
                    
                    await data_quality_guard.record_success(
                        session, "pywencai", "sector_list",
                        latency_ms=int((_time.monotonic() - t1) * 1000),
                        record_count=len(industries) + len(concepts),
                    )
                else:
                    logger.warning("pywencai映射返回空, 跳过板块列表")
            except Exception as e:
                logger.error(f"盘前pywencai板块列表失败: {e}")
                await data_quality_guard.record_failure(
                    session, "pywencai", "sector_list", str(e),
                )

            # ---- 2. 同花顺概念板块列表(补充akshare源) → SectorInfo ----
            try:
                t2 = _time.monotonic()
                df_concept = await ak_src.get_sector_list("concept")
                records = []
                for _, row in df_concept.iterrows():
                    records.append({
                        "sector_code": f"ak_concept_{row['code']}",
                        "sector_name": str(row["name"]),
                        "sector_type": "concept",
                        "source": "akshare",
                        "stock_count": None,
                    })
                await self._upsert_sector_info(session, records)
                await data_quality_guard.record_success(
                    session, "akshare", "concept_list",
                    latency_ms=int((_time.monotonic() - t2) * 1000),
                    record_count=len(records),
                )
                logger.info(f"akshare概念板块写入: {len(records)}条")
            except Exception as e:
                logger.error(f"盘前akshare概念板块失败: {e}")
                await data_quality_guard.record_failure(
                    session, "akshare", "concept_list", str(e),
                )

            # ---- 3. 同花顺行业板块列表(补充akshare源) → SectorInfo ----
            try:
                t3 = _time.monotonic()
                df_industry = await ak_src.get_sector_list("industry")
                records = []
                for _, row in df_industry.iterrows():
                    records.append({
                        "sector_code": f"ak_industry_{row['code']}",
                        "sector_name": str(row["name"]),
                        "sector_type": "industry",
                        "source": "akshare",
                        "stock_count": None,
                    })
                await self._upsert_sector_info(session, records)
                await data_quality_guard.record_success(
                    session, "akshare", "industry_list",
                    latency_ms=int((_time.monotonic() - t3) * 1000),
                    record_count=len(records),
                )
                logger.info(f"akshare行业板块写入: {len(records)}条")
            except Exception as e:
                logger.error(f"盘前akshare行业板块失败: {e}")
                await data_quality_guard.record_failure(
                    session, "akshare", "industry_list", str(e),
                )

            # ---- 4. 申万一级行业 → SectorInfo (不在板块营地展示, 但保留采集) ----
            try:
                t2 = _time.monotonic()
                df_sw1 = await sw.get_sw_index_list(1)
                # 返回: 行业代码, 行业名称, 成份个数, 静态市盈率, TTM(滚动)市盈率, 市净率, 静态股息率
                records = []
                for _, row in df_sw1.iterrows():
                    records.append({
                        "sector_code": str(row["行业代码"]),
                        "sector_name": str(row["行业名称"]),
                        "sector_type": "sw_l1",
                        "source": "shenwan",
                        "parent_code": None,
                        "stock_count": _safe_int(row.get("成份个数")),
                        "avg_pe": _safe_float(row.get("静态市盈率")),
                        "avg_pb": _safe_float(row.get("市净率")),
                        "avg_dividend": _safe_float(row.get("静态股息率")),
                    })
                await self._upsert_sector_info(session, records)
                await data_quality_guard.record_success(
                    session, "shenwan", "sw_l1_list",
                    latency_ms=int((_time.monotonic() - t2) * 1000),
                    record_count=len(records),
                )
                logger.info(f"申万一级写入: {len(records)}条")
            except Exception as e:
                logger.error(f"盘前申万一级失败: {e}")
                await data_quality_guard.record_failure(
                    session, "shenwan", "sw_l1_list", str(e),
                )

            # ---- 4. 申万二级行业 → SectorInfo ----
            try:
                t3 = _time.monotonic()
                df_sw2 = await sw.get_sw_index_list(2)
                # 返回: 行业代码, 行业名称, 上级行业, 成份个数, 静态市盈率, TTM市盈率, 市净率, 静态股息率
                records = []
                for _, row in df_sw2.iterrows():
                    # 上级行业是名称，需要反查code
                    parent_name = str(row.get("上级行业", ""))
                    parent_code = await self._find_sector_code_by_name(
                        session, parent_name, "sw_l1"
                    )
                    records.append({
                        "sector_code": str(row["行业代码"]),
                        "sector_name": str(row["行业名称"]),
                        "sector_type": "sw_l2",
                        "source": "shenwan",
                        "parent_code": parent_code,
                        "stock_count": _safe_int(row.get("成份个数")),
                        "avg_pe": _safe_float(row.get("静态市盈率")),
                        "avg_pb": _safe_float(row.get("市净率")),
                        "avg_dividend": _safe_float(row.get("静态股息率")),
                    })
                await self._upsert_sector_info(session, records)
                await data_quality_guard.record_success(
                    session, "shenwan", "sw_l2_list",
                    latency_ms=int((_time.monotonic() - t3) * 1000),
                    record_count=len(records),
                )
                logger.info(f"申万二级写入: {len(records)}条")
            except Exception as e:
                logger.error(f"盘前申万二级失败: {e}")
                await data_quality_guard.record_failure(
                    session, "shenwan", "sw_l2_list", str(e),
                )

            # ---- 5. 申万三级行业 → SectorInfo ----
            try:
                t4 = _time.monotonic()
                df_sw3 = await sw.get_sw_index_list(3)
                records = []
                for _, row in df_sw3.iterrows():
                    parent_name = str(row.get("上级行业", ""))
                    parent_code = await self._find_sector_code_by_name(
                        session, parent_name, "sw_l2"
                    )
                    records.append({
                        "sector_code": str(row["行业代码"]),
                        "sector_name": str(row["行业名称"]),
                        "sector_type": "sw_l3",
                        "source": "shenwan",
                        "parent_code": parent_code,
                        "stock_count": _safe_int(row.get("成份个数")),
                        "avg_pe": _safe_float(row.get("静态市盈率")),
                        "avg_pb": _safe_float(row.get("市净率")),
                        "avg_dividend": _safe_float(row.get("静态股息率")),
                    })
                await self._upsert_sector_info(session, records)
                await data_quality_guard.record_success(
                    session, "shenwan", "sw_l3_list",
                    latency_ms=int((_time.monotonic() - t4) * 1000),
                    record_count=len(records),
                )
                logger.info(f"申万三级写入: {len(records)}条")
            except Exception as e:
                logger.error(f"盘前申万三级失败: {e}")
                await data_quality_guard.record_failure(
                    session, "shenwan", "sw_l3_list", str(e),
                )

            # ---- 6. 新浪概念板块列表 → SectorInfo ----
            try:
                t5 = _time.monotonic()
                sina = self._sources["sina"]
                df_sina_concept = await sina.get_sector_list()
                # stock_sector_spot 返回: label, 板块, 公司家数, ...
                records = []
                for _, row in df_sina_concept.iterrows():
                    label = str(row.get("label", ""))
                    if not label.startswith("gn_"):
                        continue  # 只取概念板块
                    records.append({
                        "sector_code": f"sina_{label}",
                        "sector_name": str(row["板块"]),
                        "sector_type": "concept",
                        "source": "sina",
                        "stock_count": _safe_int(row.get("公司家数")),
                    })
                await self._upsert_sector_info(session, records)
                await data_quality_guard.record_success(
                    session, "sina", "concept_list",
                    latency_ms=int((_time.monotonic() - t5) * 1000),
                    record_count=len(records),
                )
                logger.info(f"新浪概念写入: {len(records)}条")
            except Exception as e:
                logger.error(f"盘前新浪概念失败: {e}")
                await data_quality_guard.record_failure(
                    session, "sina", "concept_list", str(e),
                )

            await session.commit()
            logger.info(f"📊 盘前数据准备完成, 耗时{_time.monotonic()-t0:.1f}s")

    async def _intraday_fast(self):
        """盘中快频 — 东财涨停/跌停/炸板 并行采集+批量写入

        优化: 3个API asyncio.gather并行(从串行6s→并行2s) + 批量upsert
        频率: 10秒/次, 端到端延时≈3秒
        """
        if not await trade_calendar.is_trading_hours():
            return

        t_total = _time.monotonic()
        em = self._sources["eastmoney"]
        today = date.today()
        today_str = today.strftime("%Y%m%d")

        # ===== 并行采集3个池 =====
        df_limit_up = pd.DataFrame()
        df_limit_down = pd.DataFrame()
        df_broken = pd.DataFrame()

        try:
            df_limit_up, df_limit_down, df_broken = await asyncio.gather(
                em.get_limit_up_pool(today_str),
                em.get_limit_down_pool(today_str),
                em.get_broken_limit_pool(today_str),
                return_exceptions=True,
            )
        except Exception as e:
            logger.error(f"盘中快频并行采集异常: {e}")
            return

        fetch_ms = int((_time.monotonic() - t_total) * 1000)

        async with async_session() as session:
            try:
                # ---- 1. 涨停池 批量写入 ----
                if isinstance(df_limit_up, pd.DataFrame) and len(df_limit_up) > 0:
                    records = self._parse_limit_up_df(df_limit_up, today, source="eastmoney")
                    if records:
                        await self._batch_upsert(
                            session, LimitUpPool, records,
                            unique_cols=["code", "trade_date"],
                            update_cols=["name", "limit_up_time", "limit_up_price",
                                         "seal_amount", "break_count", "consecutive_days",
                                         "limit_up_reason", "turnover", "source"],
                        )
                    await data_quality_guard.record_success(
                        session, "eastmoney", "limit_up",
                        latency_ms=fetch_ms, record_count=len(records),
                    )

                # ---- 2. 跌停池 批量写入 ----
                if isinstance(df_limit_down, pd.DataFrame) and len(df_limit_down) > 0:
                    records = self._parse_limit_down_df(df_limit_down, today, source="eastmoney")
                    if records:
                        await self._batch_upsert(
                            session, LimitDownPool, records,
                            unique_cols=["code", "trade_date"],
                            update_cols=["name", "limit_down_time", "break_count",
                                         "consecutive_days", "reason", "source"],
                        )
                    await data_quality_guard.record_success(
                        session, "eastmoney", "limit_down",
                        latency_ms=fetch_ms, record_count=len(records),
                    )

                # ---- 3. 炸板池 批量写入 ----
                if isinstance(df_broken, pd.DataFrame) and len(df_broken) > 0:
                    records = self._parse_broken_limit_df(df_broken, today, source="eastmoney")
                    if records:
                        await self._batch_upsert(
                            session, BrokenLimitPool, records,
                            unique_cols=["code", "trade_date"],
                            update_cols=["name", "limit_up_time", "break_time",
                                         "seal_duration", "seal_amount", "source"],
                        )
                    await data_quality_guard.record_success(
                        session, "eastmoney", "broken_limit",
                        latency_ms=fetch_ms, record_count=len(records),
                    )

                await session.commit()
                total_ms = int((_time.monotonic() - t_total) * 1000)
                logger.debug(f"盘中快频完成: 采集{fetch_ms}ms+写入{total_ms-fetch_ms}ms={total_ms}ms")

            except Exception as e:
                logger.error(f"盘中快频写入失败: {e}")
                await session.rollback()

    async def _intraday_slow(self):
        """盘中慢频 — 个股资金流+概念资金流 并行采集+批量写入

        优化: 2个API并行 + 批量upsert(5190条单条SQL)
        频率: 30秒/次, 端到端延时≈10秒
        """
        if not await trade_calendar.is_trading_hours():
            return

        t_total = _time.monotonic()
        ak_src = self._sources["akshare"]
        today = date.today()

        # ===== 并行采集: 个股资金流 + 概念资金流 =====
        df_individual = pd.DataFrame()
        df_concept = pd.DataFrame()

        try:
            df_individual, df_concept = await asyncio.gather(
                ak_src.get_individual_fund_flow(),
                ak_src.get_sector_fund_flow("概念"),
                return_exceptions=True,
            )
        except Exception as e:
            logger.error(f"盘中慢频并行采集异常: {e}")
            return

        fetch_ms = int((_time.monotonic() - t_total) * 1000)

        async with async_session() as session:
            try:
                # ---- 1. 个股资金流 批量写入 ----
                if isinstance(df_individual, pd.DataFrame) and len(df_individual) > 0:
                    records = self._parse_individual_fund_flow_df(df_individual, today)
                    if records:
                        await self._batch_upsert(
                            session, FundFlow, records,
                            unique_cols=["code", "trade_date"],
                            update_cols=["name", "main_net_inflow", "main_net_inflow_pct",
                                         "big_net_inflow", "mid_net_inflow", "small_net_inflow"],
                        )
                    await data_quality_guard.record_success(
                        session, "akshare", "individual_fund_flow",
                        latency_ms=fetch_ms, record_count=len(records),
                    )

                # ---- 2. 概念板块资金流 → 更新SectorInfo.stock_count ----
                if isinstance(df_concept, pd.DataFrame) and len(df_concept) > 0:
                    for _, row in df_concept.iterrows():
                        sector_name = str(row.get("行业", ""))
                        stock_count = _safe_int(row.get("公司家数"))
                        if sector_name and stock_count:
                            existing = await session.execute(
                                select(SectorInfo).where(
                                    SectorInfo.sector_name == sector_name,
                                    SectorInfo.source == "akshare",
                                )
                            )
                            for si in existing.scalars().all():
                                si.stock_count = stock_count
                    await session.flush()
                    await data_quality_guard.record_success(
                        session, "akshare", "concept_fund_flow",
                        latency_ms=fetch_ms, record_count=len(df_concept),
                    )

                await session.commit()
                total_ms = int((_time.monotonic() - t_total) * 1000)
                logger.debug(f"盘中慢频完成: 采集{fetch_ms}ms+写入{total_ms-fetch_ms}ms={total_ms}ms")

            except Exception as e:
                logger.error(f"盘中慢频写入失败: {e}")
                await session.rollback()

    async def _intraday_indices_and_sentiment(self):
        """指数与市场情绪快照 — 每60秒更新一次真实数据"""
        if not await trade_calendar.is_trade_day():
            return

        session_type = trade_calendar.get_trade_session()
        if session_type not in ("pre_auction", "morning", "afternoon", "after_hours"):
            return

        today = date.today()
        index_src = self._sources["index"]

        async with async_session() as session:
            try:
                # ---- 1. 三大指数(优先实时快照, 失败回退日线) ----
                index_records = []
                spot_df = None
                try:
                    spot_df = await index_src.get_index_spot()
                except Exception as e:
                    logger.warning(f"实时指数快照获取失败, 回退日线: {e}")

                spot_map = {}
                if isinstance(spot_df, pd.DataFrame) and len(spot_df) > 0:
                    for _, row in spot_df.iterrows():
                        raw_code = str(row.get("代码", ""))
                        pure_code = raw_code[-6:] if len(raw_code) >= 6 else raw_code
                        spot_map[pure_code] = row

                for code in ("000001", "399001", "399006"):
                    if code in spot_map:
                        r = spot_map[code]
                        close = _safe_float(r.get("最新价"))
                        open_ = _safe_float(r.get("今开"))
                        high = _safe_float(r.get("最高"))
                        low = _safe_float(r.get("最低"))
                        volume = _safe_float(r.get("成交量"))
                        amount = _safe_float(r.get("成交额"))
                        prev_close = _safe_float(r.get("昨收"))
                        change_pct = _safe_float(r.get("涨跌幅"))
                        amplitude = round((high - low) / prev_close * 100, 2) if prev_close and high and low else 0
                        index_records.append({
                            "code": code,
                            "trade_date": today,
                            "open": open_,
                            "high": high,
                            "low": low,
                            "close": close,
                            "volume": int(volume or 0),
                            "amount": amount,
                            "turnover": 0,
                            "amplitude": amplitude,
                            "change_pct": change_pct,
                            "prev_close": prev_close,
                        })
                        continue

                    try:
                        df = await index_src.get_index_daily(code)
                    except Exception as e:
                        logger.warning(f"指数获取失败 {code}: {e}")
                        continue

                    if df is None or len(df) == 0:
                        continue

                    df2 = df.copy()
                    if "date" not in df2.columns:
                        continue
                    df2["date"] = pd.to_datetime(df2["date"]).dt.date
                    row = df2[df2["date"] <= today].tail(1)
                    if len(row) == 0:
                        continue
                    r = row.iloc[-1]
                    trade_date = r["date"]
                    close = _safe_float(r.get("close"))
                    open_ = _safe_float(r.get("open"))
                    high = _safe_float(r.get("high"))
                    low = _safe_float(r.get("low"))
                    volume = _safe_float(r.get("volume"))
                    amount = _safe_float(r.get("amount"))

                    prev_close = None
                    hist = df2[df2["date"] < trade_date].tail(1)
                    if len(hist) > 0:
                        prev_close = _safe_float(hist.iloc[-1].get("close"))
                    if not prev_close and open_:
                        prev_close = open_

                    change_pct = 0.0
                    if prev_close:
                        change_pct = round((close - prev_close) / prev_close * 100, 2)

                    index_records.append({
                        "code": code,
                        "trade_date": trade_date,
                        "open": open_,
                        "high": high,
                        "low": low,
                        "close": close,
                        "volume": int(volume or 0),
                        "amount": amount,
                        "turnover": 0,
                        "amplitude": round((high - low) / prev_close * 100, 2) if prev_close and high and low else 0,
                        "change_pct": change_pct,
                        "prev_close": prev_close,
                    })

                if index_records:
                    await self._batch_upsert(
                        session, StockDaily, index_records,
                        unique_cols=["code", "trade_date"],
                        update_cols=["open", "high", "low", "close", "volume", "amount",
                                     "turnover", "amplitude", "change_pct", "prev_close"],
                    )
                    await data_quality_guard.record_success(
                        session, "index", "daily_snapshot", latency_ms=0, record_count=len(index_records)
                    )

                # ---- 2. 市场情绪 ----
                lu_result = await session.execute(
                    select(LimitUpPool).where(LimitUpPool.trade_date == today)
                )
                ld_result = await session.execute(
                    select(LimitDownPool).where(LimitDownPool.trade_date == today)
                )
                bl_result = await session.execute(
                    select(BrokenLimitPool).where(BrokenLimitPool.trade_date == today)
                )
                ff_result = await session.execute(
                    select(FundFlow.main_net_inflow).where(FundFlow.trade_date == today)
                )

                limit_ups = lu_result.scalars().all()
                limit_downs = ld_result.scalars().all()
                broken_limits = bl_result.scalars().all()
                main_flows = [r[0] or 0 for r in ff_result.all()]

                lu_count = len(limit_ups)
                ld_count = len(limit_downs)
                bl_count = len(broken_limits)
                seal_rate = round(lu_count / (lu_count + bl_count) * 100, 1) if (lu_count + bl_count) > 0 else 0
                board_height = max([lu.consecutive_days or 1 for lu in limit_ups], default=0)
                main_net_inflow = round(sum(main_flows) / 1e8, 2) if main_flows else 0
                advance_decline_ratio = round(lu_count / ld_count, 2) if ld_count > 0 else float(lu_count)

                score = 0
                if lu_count >= 80:
                    score += 2
                elif lu_count >= 50:
                    score += 1
                elif lu_count < 20:
                    score -= 2
                elif lu_count < 30:
                    score -= 1

                if seal_rate >= 80:
                    score += 2
                elif seal_rate >= 65:
                    score += 1
                elif seal_rate < 45:
                    score -= 2
                elif seal_rate < 60:
                    score -= 1

                if board_height >= 5:
                    score += 2
                elif board_height >= 3:
                    score += 1
                elif board_height <= 1:
                    score -= 1

                if ld_count >= 20:
                    score -= 2
                elif ld_count >= 10:
                    score -= 1

                if bl_count >= lu_count and bl_count >= 20:
                    score -= 2
                elif bl_count >= max(10, lu_count * 0.5):
                    score -= 1

                if main_net_inflow >= 80:
                    score += 1
                elif main_net_inflow <= -80:
                    score -= 1

                if score >= 4:
                    sentiment_cycle = "climax"
                elif score >= 1:
                    sentiment_cycle = "recovery"
                elif score <= -4:
                    sentiment_cycle = "freezing"
                else:
                    sentiment_cycle = "divergence"

                sentiment_record = {
                    "trade_date": today,
                    "sentiment_cycle": sentiment_cycle,
                    "limit_up_count": lu_count,
                    "limit_down_count": ld_count,
                    "broken_limit_count": bl_count,
                    "seal_rate": seal_rate,
                    "board_height": board_height,
                    "advance_decline_ratio": advance_decline_ratio,
                    "turnover_total": 0,
                    "main_net_inflow": main_net_inflow,
                }
                await self._batch_upsert(
                    session, MarketSentiment, [sentiment_record],
                    unique_cols=["trade_date"],
                    update_cols=["sentiment_cycle", "limit_up_count", "limit_down_count",
                                 "broken_limit_count", "seal_rate", "board_height",
                                 "advance_decline_ratio", "turnover_total", "main_net_inflow"],
                )
                await data_quality_guard.record_success(
                    session, "market_sentiment", "snapshot", latency_ms=0, record_count=1
                )

                await session.commit()
                logger.debug(f"指数与情绪快照更新完成: 指数{len(index_records)}条, 情绪1条")

            except Exception as e:
                logger.error(f"指数与情绪快照失败: {e}")
                await session.rollback()

    async def _dashboard2_snapshot(self):
        """overview-v2 快照刷新"""
        from app.dashboard2.jobs import refresh_dashboard2_snapshot
        await refresh_dashboard2_snapshot()

    async def _intraday_sina(self):
        """盘中新浪 — 概念板块实时行情(5分钟/次)

        更新SectorInfo的stock_count + SectorPersistence的fund_flow
        """
        if not await trade_calendar.is_trading_hours():
            return

        async with async_session() as session:
            sina = self._sources["sina"]
            try:
                t0 = _time.monotonic()
                df = await sina.get_sector_list()
                if df is not None and len(df) > 0:
                    for _, row in df.iterrows():
                        label = str(row.get("label", ""))
                        if not label.startswith("gn_"):
                            continue
                        sector_name = str(row.get("板块", ""))
                        stock_count = _safe_int(row.get("公司家数"))
                        if sector_name and stock_count:
                            existing = await session.execute(
                                select(SectorInfo).where(
                                    SectorInfo.sector_name == sector_name,
                                    SectorInfo.source == "sina",
                                )
                            )
                            for si in existing.scalars().all():
                                si.stock_count = stock_count

                    await session.commit()
                    await data_quality_guard.record_success(
                        session, "sina", "concept_spot",
                        latency_ms=int((_time.monotonic() - t0) * 1000),
                        record_count=len(df),
                    )
                    logger.debug(f"新浪概念行情写入: {len(df)}条")
            except Exception as e:
                logger.error(f"盘中新浪概念采集失败: {e}")

    async def _auction_collect(self):
        """竞价采集 — 9:15-9:25每30秒采集一次

        使用ak.stock_zh_a_spot_em全A快照, 提取竞价信息
        """
        # 只在竞价时段执行
        session_type = trade_calendar.get_trade_session()
        if session_type != "pre_auction":
            return

        if not await trade_calendar.is_trade_day():
            return

        from app.strategy.auction import auction_scheduler
        async with async_session() as session:
            try:
                result = await auction_scheduler.run_auction_phase(session)
                logger.debug(f"竞价采集: {result.get('status')}, {result.get('total_signals', 0)}异动")
            except Exception as e:
                logger.error(f"竞价采集失败: {e}")

    async def _after_market(self):
        """盘后补全 — pywencai个股行业映射 + 涨停池补充"""
        if not await trade_calendar.is_trade_day():
            return
        logger.info("📊 盘后数据补全...")
        async with async_session() as session:
            pw = self._sources["pywencai"]
            today = date.today()

            # ---- 1. pywencai 个股→行业+概念映射 ----
            try:
                t0 = _time.monotonic()
                df = await pw.get_stock_industry_mapping()
                # pywencai.get("全部A股 所属同花顺行业 所属概念") 返回列:
                #   股票代码, 股票简称, 所属同花顺行业, 所属概念(多个逗号分隔)
                if df is not None and len(df) > 0:
                    mapping_records = []
                    for _, row in df.iterrows():
                        code = _normalize_code(str(row.get("股票代码", "")))
                        name = str(row.get("股票简称", ""))
                        industry = str(row.get("所属同花顺行业", ""))
                        concepts = str(row.get("所属概念", ""))

                        if not code:
                            continue

                        # 行业映射(pywencai三级格式"医药生物-中药-中药Ⅲ", 用三级全名与SectorInfo一致)
                        if industry and industry != "nan":
                            industry = industry.strip()
                            mapping_records.append({
                                "code": code,
                                "sector_code": f"pw_industry_{industry}",
                                "sector_name": industry,
                                "sector_type": "industry",
                                "source": "pywencai",
                                "weight": None,
                            })

                        # 概念映射(多值分号分隔, pywencai用分号";")
                        if concepts and concepts != "nan":
                            for concept in concepts.split(";"):
                                concept = concept.strip()
                                if concept:
                                    mapping_records.append({
                                        "code": code,
                                        "sector_code": f"pw_concept_{concept}",
                                        "sector_name": concept,
                                        "sector_type": "concept",
                                        "source": "pywencai",
                                        "weight": None,
                                    })

                    # 分批写入(每100条)
                    batch_size = 100
                    for i in range(0, len(mapping_records), batch_size):
                        await self._upsert_stock_sector_mapping(
                            session, mapping_records[i:i+batch_size]
                        )
                    await data_quality_guard.record_success(
                        session, "pywencai", "stock_mapping",
                        latency_ms=int((_time.monotonic() - t0) * 1000),
                        record_count=len(mapping_records),
                    )
                    logger.info(f"个股映射写入: {len(mapping_records)}条")

                    # 同时打股票标记
                    stocks_for_tag = []
                    for _, row in df.iterrows():
                        code = _normalize_code(str(row.get("股票代码", "")))
                        name = str(row.get("股票简称", ""))
                        if code:
                            is_st = "ST" in name or "*ST" in name
                            stocks_for_tag.append({
                                "code": code,
                                "name": name,
                                "is_st": is_st,
                            })
                    await stock_tagger.batch_tag(session, stocks_for_tag)
                    logger.info(f"股票标记写入: {len(stocks_for_tag)}只")
                else:
                    logger.warning("pywencai映射返回空DataFrame")
            except Exception as e:
                logger.error(f"盘后映射补全失败: {e}")
                await data_quality_guard.record_failure(
                    session, "pywencai", "stock_mapping", str(e),
                )

            # ---- 2. pywencai 涨停池补充 ----
            try:
                t1 = _time.monotonic()
                df = await pw.get_limit_up_pool()
                # pywencai.get("涨停 连板数") 返回列:
                #   股票代码, 股票简称, 涨停价, 涨跌幅, 连板数, 封板资金, ...
                if df is not None and len(df) > 0:
                    records = self._parse_limit_up_df(df, today, source="pywencai")
                    await self._upsert_limit_up(session, records)
                    await data_quality_guard.record_success(
                        session, "pywencai", "limit_up",
                        latency_ms=int((_time.monotonic() - t1) * 1000),
                        record_count=len(records),
                    )
                    logger.info(f"pywencai涨停写入: {len(records)}条")
            except Exception as e:
                logger.error(f"盘后pywencai涨停失败: {e}")

            await session.commit()

    async def _deep_review(self):
        """盘后深度 — 申万个股行业映射 → StockSectorMapping"""
        logger.info("📊 盘后深度数据采集...")
        async with async_session() as session:
            sw = self._sources["shenwan"]

            # ---- 1. 申万个股行业映射 ----
            try:
                t0 = _time.monotonic()
                df = await sw.get_stock_industry_clf()
                # ak.stock_industry_clf_hist_sw 返回列:
                #   symbol(股票代码), start_date, industry_code(申万行业代码), update_time
                # 注意: 同一只股票可能有多条记录(行业调整)，取最新的
                if df is not None and len(df) > 0:
                    # 按symbol分组，取最新一条
                    df_sorted = df.sort_values("update_time", ascending=False)
                    latest = df_sorted.drop_duplicates(subset=["symbol"], keep="first")

                    # 先加载所有申万行业信息，用于反查sector_name
                    sw_sectors = await session.execute(select(SectorInfo).where(
                        SectorInfo.source == "shenwan"
                    ))
                    sector_map = {s.sector_code: s.sector_name for s in sw_sectors.scalars().all()}

                    mapping_records = []
                    for _, row in latest.iterrows():
                        code = _normalize_code(str(row.get("symbol", "")))
                        industry_code = str(row.get("industry_code", ""))
                        if not code or not industry_code:
                            continue

                        # 从行业代码推断层级
                        if industry_code.endswith("01") and len(industry_code) == 6:
                            sector_type = "sw_l1"
                        elif len(industry_code) == 6:
                            sector_type = "sw_l2"
                        elif len(industry_code) == 8:
                            sector_type = "sw_l3"
                        else:
                            sector_type = "sw_l1"

                        sector_name = sector_map.get(industry_code, "")

                        mapping_records.append({
                            "code": code,
                            "sector_code": industry_code,
                            "sector_name": sector_name,
                            "sector_type": sector_type,
                            "source": "shenwan",
                            "weight": None,
                        })

                    # 分批写入
                    batch_size = 100
                    for i in range(0, len(mapping_records), batch_size):
                        await self._upsert_stock_sector_mapping(
                            session, mapping_records[i:i+batch_size]
                        )
                    await data_quality_guard.record_success(
                        session, "shenwan", "stock_mapping",
                        latency_ms=int((_time.monotonic() - t0) * 1000),
                        record_count=len(mapping_records),
                    )
                    logger.info(f"申万映射写入: {len(mapping_records)}条")
            except Exception as e:
                logger.error(f"深度申万映射失败: {e}")
                await data_quality_guard.record_failure(
                    session, "shenwan", "stock_mapping", str(e),
                )

            await session.commit()

    async def _quality_check(self):
        """数据质量检查"""
        async with async_session() as session:
            for name, source in self._sources.items():
                try:
                    is_ok = await source.health_check(session)
                    if not is_ok:
                        logger.warning(f"⚠️ 数据源 {name} 健康检查失败")
                except Exception as e:
                    logger.error(f"数据源 {name} 健康检查异常: {e}")

    # =========================================================================
    # DataFrame 解析器 (AkShare列名 → ORM字段映射)
    # =========================================================================

    @staticmethod
    def _parse_limit_up_df(df: pd.DataFrame, trade_date: date,
                            source: str = "eastmoney") -> list[dict]:
        """解析涨停池 DataFrame → LimitUpPool 记录列表

        东财 ak.stock_zt_pool_em 标准列:
          代码, 名称, 涨停价, 最新价, 涨跌幅, 成交额,
          流通市值, 封板资金, 首次封板时间, 最后封板时间,
          炸板次数, 连板数, 涨停统计

        pywencai 涨停池列:
          股票代码, 股票简称, 涨停价, 涨跌幅, 连板数, 封板资金, ...
        """
        records = []
        for _, row in df.iterrows():
            # 兼容两种来源的列名
            code = _normalize_code(
                str(row.get("代码", row.get("股票代码", "")))
            )
            name = str(row.get("名称", row.get("股票简称", "")))
            limit_price = _safe_float(row.get("涨停价"))
            seal_amt = _safe_float(row.get("封板资金"))
            break_cnt = _safe_int(row.get("炸板次数", 0))
            consec = _safe_int(row.get("连板数", 1))
            turnover = _safe_float(row.get("换手率"))

            # 涨停时间: 取首次封板时间
            limit_time = str(row.get("首次封板时间", row.get("涨停时间", "")))
            if limit_time == "nan":
                limit_time = ""

            # 涨停原因
            reason = str(row.get("涨停原因", row.get("涨停统计", "")))
            if reason == "nan":
                reason = ""

            if not code:
                continue

            records.append({
                "code": code,
                "name": name,
                "trade_date": trade_date,
                "limit_up_time": limit_time,
                "limit_up_price": limit_price,
                "seal_amount": seal_amt,
                "break_count": break_cnt,
                "consecutive_days": consec,
                "limit_up_reason": reason if reason else None,
                "turnover": turnover,
                "source": source,
            })
        return records

    @staticmethod
    def _parse_limit_down_df(df: pd.DataFrame, trade_date: date,
                              source: str = "eastmoney") -> list[dict]:
        """解析跌停池 DataFrame → LimitDownPool 记录列表

        东财 ak.stock_zt_pool_dtgc_em 标准列:
          代码, 名称, 跌停价, 最新价, 涨跌幅, 成交额, ...
        """
        records = []
        for _, row in df.iterrows():
            code = _normalize_code(
                str(row.get("代码", row.get("股票代码", "")))
            )
            name = str(row.get("名称", row.get("股票简称", "")))
            limit_time = str(row.get("跌停时间", ""))
            if limit_time == "nan":
                limit_time = ""
            break_cnt = _safe_int(row.get("炸板次数", 0))
            consec = _safe_int(row.get("连续跌停", 1))
            reason = str(row.get("跌停原因", ""))

            if not code:
                continue

            records.append({
                "code": code,
                "name": name,
                "trade_date": trade_date,
                "limit_down_time": limit_time if limit_time else None,
                "break_count": break_cnt,
                "consecutive_days": consec,
                "reason": reason if reason != "nan" else None,
                "source": source,
            })
        return records

    @staticmethod
    def _parse_broken_limit_df(df: pd.DataFrame, trade_date: date,
                                source: str = "eastmoney") -> list[dict]:
        """解析炸板池 DataFrame → BrokenLimitPool 记录列表

        东财 ak.stock_zt_pool_zbgc_em 标准列:
          代码, 名称, 涨停价, 最新价, 涨跌幅, 成交额,
          首次封板时间, 最后封板时间, 炸板次数, 封板资金, ...
        """
        records = []
        for _, row in df.iterrows():
            code = _normalize_code(
                str(row.get("代码", row.get("股票代码", "")))
            )
            name = str(row.get("名称", row.get("股票简称", "")))
            limit_time = str(row.get("首次封板时间", ""))
            break_time = str(row.get("最后封板时间", ""))
            if limit_time == "nan":
                limit_time = ""
            if break_time == "nan":
                break_time = ""
            seal_amt = _safe_float(row.get("封板资金"))

            if not code:
                continue

            records.append({
                "code": code,
                "name": name,
                "trade_date": trade_date,
                "limit_up_time": limit_time if limit_time else None,
                "break_time": break_time if break_time else None,
                "seal_duration": None,
                "seal_amount": seal_amt,
                "source": source,
            })
        return records

    @staticmethod
    def _parse_individual_fund_flow_df(df: pd.DataFrame,
                                        trade_date: date) -> list[dict]:
        """解析个股资金流 DataFrame → FundFlow 记录列表

        ak.stock_individual_fund_flow_rank 返回列:
          代码, 名称, 最新价, 涨跌幅, 换手率,
          主力净流入-净额, 主力净流入-净占比,
          超大单净流入-净额, 超大单净流入-净占比,
          大单净流入-净额, 大单净流入-净占比,
          中单净流入-净额, 中单净流入-净占比,
          小单净流入-净额, 小单净流入-净占比
        """
        records = []
        for _, row in df.iterrows():
            code = _normalize_code(str(row.get("代码", "")))
            name = str(row.get("名称", ""))

            if not code:
                continue

            # 主力 = 超大单 + 大单
            main_inflow = _safe_float(row.get("主力净流入-净额"))
            main_pct = _safe_float(row.get("主力净流入-净占比"))
            big_inflow = _safe_float(row.get("大单净流入-净额"))
            mid_inflow = _safe_float(row.get("中单净流入-净额"))
            small_inflow = _safe_float(row.get("小单净流入-净额"))

            records.append({
                "code": code,
                "name": name,
                "trade_date": trade_date,
                "main_net_inflow": main_inflow,
                "main_net_inflow_pct": main_pct,
                "big_net_inflow": big_inflow,
                "mid_net_inflow": mid_inflow,
                "small_net_inflow": small_inflow,
            })
        return records

    # =========================================================================
    # 辅助方法
    # =========================================================================

    @staticmethod
    async def _find_sector_code_by_name(session: AsyncSession,
                                         sector_name: str,
                                         sector_type: str) -> Optional[str]:
        """根据板块名称和类型反查 sector_code"""
        if not sector_name or sector_name == "nan":
            return None
        result = await session.execute(
            select(SectorInfo).where(
                SectorInfo.sector_name == sector_name,
                SectorInfo.sector_type == sector_type,
            ).limit(1)
        )
        row = result.scalar_one_or_none()
        return row.sector_code if row else None

    def start(self):
        """启动调度器"""
        self.setup_jobs()
        self.scheduler.start()
        logger.info("🦅 采集调度器已启动")

    def stop(self):
        """停止调度器"""
        self.scheduler.shutdown()
        logger.info("采集调度器已停止")

    # =========================================================================
    # 派生计算: 板块持续性+强弱+生命周期
    # =========================================================================

    async def _sector_derive(self):
        """板块派生计算 — 从原始数据计算持续性/强弱/生命周期

        每2分钟运行一次, 仅在交易时段执行:
        1. 概念资金流 → SectorPersistence (fund_flow/change_pct/strength_score)
        2. 涨停分布 → SectorPersistence (limit_up_count/consecutive_days)
        3. SectorRotationEngine → SectorStrength (强弱排名)
        4. SectorLifecycleAnalyzer → SectorLifecycle (生命周期状态)
        """
        if not await trade_calendar.is_trading_hours():
            return

        today = date.today()
        async with async_session() as session:
            try:
                t0 = _time.monotonic()

                # ---- 1. 从概念资金流更新 SectorPersistence ----
                await self._update_sector_persistence(session, today)

                # ---- 2. 计算板块强弱排名 ----
                from app.risk.rotation import SectorRotationEngine
                engine = SectorRotationEngine()
                items = await engine.calc_sector_strength(session, today)
                if items:
                    await engine.save_strength_ranking(session, items, today)

                # ---- 3. 计算生命周期(只算有涨停的板块, 避免全量计算) ----
                await self._compute_lifecycle(session, today)

                await session.commit()
                total_ms = int((_time.monotonic() - t0) * 1000)
                logger.debug(f"板块派生计算完成: {total_ms}ms")
            except Exception as e:
                logger.error(f"板块派生计算失败: {e}")
                await session.rollback()

    async def _update_sector_persistence(self, session: AsyncSession, trade_date: date):
        """从概念资金流+涨停数据更新 SectorPersistence

        逻辑:
        - 概念资金流(AkShare采集的) → fund_flow / change_pct / strength_score
        - 涨停池(LimitUpPool) → limit_up_count (按板块统计)
        - 连续天数 → 和前一交易日比较
        """
        from sqlalchemy import func as sa_func, and_, text as sa_text

        # 获取概念板块资金流(行业资金流可能报错, 容错)
        ak_src = self._sources["akshare"]
        try:
            df_concept = await ak_src.get_sector_fund_flow("概念")
        except Exception as e:
            logger.warning(f"概念资金流获取失败: {e}")
            df_concept = pd.DataFrame()
        try:
            df_industry = await ak_src.get_sector_fund_flow("行业")
        except Exception as e:
            logger.warning(f"行业资金流获取失败: {e}")
            df_industry = pd.DataFrame()

        # 获取今日涨停按板块分布
        limit_up_result = await session.execute(
            select(LimitUpPool).where(LimitUpPool.trade_date == trade_date)
        )
        limit_ups = limit_up_result.scalars().all()

        # 统计每个板块的涨停数(从stock_sector_mapping)
        limit_up_by_sector = {}  # sector_code → count
        if limit_ups:
            codes = [lu.code for lu in limit_ups if lu.code]
            if codes:
                mapping_result = await session.execute(
                    select(StockSectorMapping.sector_code, sa_func.count())
                    .where(StockSectorMapping.code.in_(codes))
                    .group_by(StockSectorMapping.sector_code)
                )
                limit_up_by_sector = {r[0]: r[1] for r in mapping_result.all()}

        # 获取前一交易日的SectorPersistence(用于计算consecutive_days)
        prev_result = await session.execute(
            select(SectorPersistence)
            .where(SectorPersistence.trade_date < trade_date)
            .order_by(SectorPersistence.trade_date.desc())
            .limit(654)  # 全量板块
        )
        prev_records = prev_result.scalars().all()
        prev_map = {r.sector_code: r for r in prev_records}

        # 预加载SectorInfo建立名称映射(行业一二级名→三级sector_code列表)
        si_result = await session.execute(
            select(SectorInfo.sector_code, SectorInfo.sector_name, SectorInfo.sector_type)
            .where(SectorInfo.source == "pywencai")
        )
        all_si = si_result.all()
        # 精确名称映射: (sector_name, sector_type) → sector_code
        si_name_map = {}
        # 行业子映射: 一级或二级名 → [sector_code, ...]
        industry_sub_map = {}
        for si_row in all_si:
            si_name_map[(si_row.sector_name, si_row.sector_type)] = si_row.sector_code
            # 概念去空格
            clean_key = (si_row.sector_name.replace(" ", "").replace("\u3000", ""), si_row.sector_type)
            if clean_key not in si_name_map:
                si_name_map[clean_key] = si_row.sector_code
            if si_row.sector_type == "industry":
                parts = si_row.sector_name.split("-")
                # 一级名(如"电力设备")
                l1 = parts[0] if parts else si_row.sector_name
                if l1 not in industry_sub_map:
                    industry_sub_map[l1] = []
                industry_sub_map[l1].append(si_row.sector_code)
                # 二级名(如"电池"→"电力设备-电池")
                if len(parts) >= 2:
                    l2 = parts[1]
                    if l2 not in industry_sub_map:
                        industry_sub_map[l2] = []
                    industry_sub_map[l2].append(si_row.sector_code)

        # 更新概念+行业板块
        records = []
        for df, sector_type in [(df_concept, "concept"), (df_industry, "industry")]:
            if df is None or not isinstance(df, pd.DataFrame) or len(df) == 0:
                continue

            for _, row in df.iterrows():
                sector_name = str(row.get("行业", "") or row.get("名称", ""))
                if not sector_name or sector_name == "nan":
                    continue

                fund_flow = _safe_float(row.get("净额", row.get("主力净流入-净额", row.get("今日主力净流入", row.get("主力净流入", 0)))))
                change_pct = _safe_float(row.get("行业-涨跌幅", row.get("今日涨跌幅", row.get("涨跌幅", row.get("行业涨跌幅", 0)))))
                stock_count = _safe_int(row.get("公司家数", row.get("个股数", 0)))

                # 匹配sector_code: 精确匹配优先，行业模糊匹配(一二级→三级)
                matched_code = si_name_map.get((sector_name, sector_type))
                if not matched_code:
                    clean = sector_name.replace(" ", "").replace("\u3000", "")
                    matched_code = si_name_map.get((clean, sector_type))

                if matched_code:
                    # 精确匹配: 单条写入
                    lu_count = limit_up_by_sector.get(matched_code, 0)
                    prev = prev_map.get(matched_code)
                    if prev and ((prev.limit_up_count or 0) > 0 or (prev.fund_flow or 0) > 0):
                        consecutive_days = (prev.consecutive_days or 0) + 1
                    elif lu_count > 0 or (fund_flow and fund_flow > 0):
                        consecutive_days = 1
                    else:
                        consecutive_days = 0
                    score = 0
                    if fund_flow is not None:
                        if fund_flow > 20: score += 40
                        elif fund_flow > 5: score += 30
                        elif fund_flow > 0: score += 15
                    if change_pct is not None:
                        score += min(max(change_pct * 5, -20), 30)
                    if lu_count > 0:
                        score += min(lu_count * 5, 30)
                    score = max(0, min(100, round(score, 1)))

                    # 获取DB中的正确sector_name
                    db_name = sector_name
                    for si_row in all_si:
                        if si_row.sector_code == matched_code:
                            db_name = si_row.sector_name
                            break

                    records.append({
                        "sector_code": matched_code,
                        "sector_name": db_name,
                        "trade_date": trade_date,
                        "consecutive_days": consecutive_days,
                        "limit_up_count": lu_count,
                        "fund_flow": round(fund_flow or 0, 2),
                        "change_pct": round(change_pct or 0, 2),
                        "strength_score": score,
                    })
                elif sector_type == "industry":
                    # 行业名匹配一级/二级: 将资金流均匀分配到子行业
                    sub_codes = industry_sub_map.get(sector_name, [])
                    if sub_codes:
                        per_code_flow = round(fund_flow / len(sub_codes), 2) if fund_flow else 0
                        for sc in sub_codes:
                            lu_count = limit_up_by_sector.get(sc, 0)
                            prev = prev_map.get(sc)
                            if prev and ((prev.limit_up_count or 0) > 0 or (prev.fund_flow or 0) > 0):
                                consecutive_days = (prev.consecutive_days or 0) + 1
                            elif lu_count > 0 or (per_code_flow and per_code_flow > 0):
                                consecutive_days = 1
                            else:
                                consecutive_days = 0
                            sub_score = 0
                            if per_code_flow is not None:
                                if per_code_flow > 20: sub_score += 40
                                elif per_code_flow > 5: sub_score += 30
                                elif per_code_flow > 0: sub_score += 15
                            if change_pct is not None:
                                sub_score += min(max(change_pct * 5, -20), 30)
                            if lu_count > 0:
                                sub_score += min(lu_count * 5, 30)
                            sub_score = max(0, min(100, round(sub_score, 1)))

                            db_name = sector_name
                            for si_row in all_si:
                                if si_row.sector_code == sc:
                                    db_name = si_row.sector_name
                                    break

                            records.append({
                                "sector_code": sc,
                                "sector_name": db_name,
                                "trade_date": trade_date,
                                "consecutive_days": consecutive_days,
                                "limit_up_count": lu_count,
                                "fund_flow": round(per_code_flow or 0, 2),
                                "change_pct": round(change_pct or 0, 2),
                                "strength_score": sub_score,
                            })

        if records:
            await self._batch_upsert(
                session, SectorPersistence, records,
                unique_cols=["sector_code", "trade_date"],
                update_cols=["sector_name", "consecutive_days", "limit_up_count",
                             "fund_flow", "change_pct", "strength_score"],
            )
            logger.info(f"板块持续性更新: {len(records)}条")

    async def _compute_lifecycle(self, session: AsyncSession, trade_date: date):
        """计算板块生命周期 — 对有SectorPersistence数据的板块计算(概念+行业)"""
        from app.sector.lifecycle import SectorLifecycleEngine
        from app.models.sector import SectorLifecycle
        from sqlalchemy import and_

        analyzer = SectorLifecycleEngine()

        # 获取当日有SectorPersistence数据的板块(概念+行业)
        persist_result = await session.execute(
            select(SectorPersistence.sector_code, SectorPersistence.sector_name)
            .where(SectorPersistence.trade_date == trade_date)
            .order_by(desc(SectorPersistence.strength_score))
            .limit(100)  # 限制最多100个, 避免太慢
        )
        persist_sectors = persist_result.all()

        if not persist_sectors:
            return

        # 获取板块类型映射
        si_result = await session.execute(
            select(SectorInfo.sector_code, SectorInfo.sector_type, SectorInfo.sector_name)
            .where(SectorInfo.source == "pywencai")
        )
        si_map = {r.sector_code: r for r in si_result.all()}

        count = 0
        for sector_code, sector_name in persist_sectors:
            si = si_map.get(sector_code)
            if not si:
                continue
            sector_type = si.sector_type
            # 使用SectorInfo中的正确名称(防止kline_name泄漏)
            db_name = si.sector_name
            try:
                data = await analyzer.analyze_sector(
                    session, sector_code, db_name, sector_type, trade_date,
                )
                await analyzer.save_lifecycle(session, data)
                count += 1
            except Exception as e:
                logger.debug(f"板块生命周期计算失败 {db_name}: {e}")

        if count:
            logger.info(f"板块生命周期计算: {count}个板块")


# =============================================================================
# 通用工具函数
# =============================================================================

def _normalize_code(raw: str) -> str:
    """标准化股票代码

    输入: '1', '000001', '600519', 'sh600519', 'sz000001', '600777.SH'
    输出: '000001', '600519'  (纯6位数字)
    """
    raw = raw.strip()
    # 去掉市场后缀 (.SH/.SZ/.BJ)
    for suffix in (".SH", ".SZ", ".BJ", ".sh", ".sz", ".bj"):
        if raw.upper().endswith(suffix):
            raw = raw[:-3]
            break
    # 去掉市场前缀
    for prefix in ("sh", "sz", "bj", "SH", "SZ", "BJ"):
        if raw.lower().startswith(prefix):
            raw = raw[2:]
            break
    # 补齐6位
    if raw.isdigit():
        return raw.zfill(6)
    return raw


def _safe_float(val) -> Optional[float]:
    """安全转 float, NaN/None/空字符串 → None"""
    if val is None:
        return None
    try:
        f = float(val)
        return f if pd.notna(f) else None
    except (ValueError, TypeError):
        return None


def _safe_int(val) -> Optional[int]:
    """安全转 int, NaN/None/空字符串 → None"""
    if val is None:
        return None
    try:
        f = float(val)
        return int(f) if pd.notna(f) else None
    except (ValueError, TypeError):
        return None



# 全局单例
data_scheduler = DataScheduler()
