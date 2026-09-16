"""Dashboard 2.0 聚合服务"""

import asyncio
from datetime import datetime, date
from sqlalchemy import select, func, desc, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.stock import (
    StockDaily,
    MarketSentiment,
    SectorPersistence,
    SectorInfo,
    LimitUpPool,
    LimitDownPool,
)
from app.risk.circuit_breaker import sentiment_circuit_breaker
from .mapping import GLOBAL_TO_CN_SECTOR_MAPPING
from .schemas import (
    Dashboard2Snapshot,
    MappingInsightItem,
    OverviewConclusionItem,
    FocusStripItem,
    AShareCoreState,
    AShareIndexItem,
)
from .external_sources import external_factor_collector


class Dashboard2Service:
    THEME_KEYWORD_EXPANSION = {
        "成长风格": ["算力", "半导体", "AI应用", "软件服务", "传媒", "创业板"],
        "科技股": ["算力", "半导体", "CPO", "AI应用", "软件服务"],
        "AI链": ["算力", "CPO", "液冷", "服务器", "先进封装", "半导体", "AI应用"],
        "全球风险偏好": ["证券", "传媒", "消费电子", "有色", "白酒"],
        "权重白马": ["银行", "保险", "白酒", "家电", "中字头"],
        "核心资产": ["银行", "保险", "白酒", "家电", "医药"],
        "外资权重": ["银行", "保险", "白酒", "家电", "医药"],
        "出口链": ["航运", "家电", "汽车零部件", "纺织", "机械"],
        "风险偏好": ["证券", "传媒", "半导体", "消费电子"],
        "金融地产": ["银行", "保险", "房地产", "建筑"],
        "上证50": ["银行", "保险", "白酒", "中字头", "煤炭"],
        "沪深300": ["银行", "保险", "白酒", "家电", "有色"],
        "高估值科技": ["半导体", "算力", "CPO", "软件服务", "AI应用", "创业板"],
    }
    INDEX_CODE_MAP = {
        "000001": ["000001", "000001.SH", "sh000001", "SH000001"],
        "399001": ["399001", "399001.SZ", "sz399001", "SZ399001"],
        "399006": ["399006", "399006.SZ", "sz399006", "SZ399006"],
    }
    SENTIMENT_CYCLE_LABELS = {
        "recovery": "修复",
        "climax": "亢奋",
        "divergence": "分歧",
        "freezing": "冰点",
        "pending": "待定",
    }

    async def _resolve_trade_date(self, db: AsyncSession) -> date:
        """口径日期：优先当日，盘后/非交易时段自动回退最近交易日。"""
        today = date.today()
        stock_latest = (
            await db.execute(
                select(func.max(StockDaily.trade_date)).where(StockDaily.trade_date <= today)
            )
        ).scalar_one_or_none()
        sentiment_latest = (
            await db.execute(
                select(func.max(MarketSentiment.trade_date)).where(MarketSentiment.trade_date <= today)
            )
        ).scalar_one_or_none()

        candidates = [d for d in [stock_latest, sentiment_latest] if d is not None]
        return max(candidates) if candidates else today

    async def _query_latest_index(self, db: AsyncSession, code: str, as_of: date) -> StockDaily | None:
        aliases = self.INDEX_CODE_MAP.get(code, [code])

        # 1) 精确别名匹配
        for alias in aliases:
            row = (
                await db.execute(
                    select(StockDaily)
                    .where(
                        StockDaily.code == alias,
                        StockDaily.trade_date <= as_of,
                    )
                    .order_by(desc(StockDaily.trade_date))
                    .limit(1)
                )
            ).scalar_one_or_none()
            if row:
                return row

        # 2) 模糊兜底（处理历史脏数据代码格式）
        return (
            await db.execute(
                select(StockDaily)
                .where(
                    StockDaily.code.like(f"%{code}%"),
                    StockDaily.trade_date <= as_of,
                )
                .order_by(desc(StockDaily.trade_date))
                .limit(1)
            )
        ).scalar_one_or_none()

    async def _load_a_share_context(self, db: AsyncSession) -> dict:
        as_of = await self._resolve_trade_date(db)

        sh = await self._query_latest_index(db, "000001", as_of)
        sz = await self._query_latest_index(db, "399001", as_of)
        cyb = await self._query_latest_index(db, "399006", as_of)

        sentiment = (
            await db.execute(
                select(MarketSentiment)
                .where(MarketSentiment.trade_date <= as_of)
                .order_by(desc(MarketSentiment.trade_date))
                .limit(1)
            )
        ).scalar_one_or_none()

        sentiment_state = await sentiment_circuit_breaker.get_current_state(db, as_of)

        limit_up_count = (
            await db.execute(
                select(func.count(LimitUpPool.id)).where(LimitUpPool.trade_date == as_of)
            )
        ).scalar() or 0
        limit_down_count = (
            await db.execute(
                select(func.count(LimitDownPool.id)).where(LimitDownPool.trade_date == as_of)
            )
        ).scalar() or 0

        # 总览任务每分钟执行，这里只聚合调度器已入库的统一口径（亿）。
        # 不在请求/快照链路里重复抓取东财，避免慢网络调用拖累全站接口。
        persisted_main_inflow = getattr(sentiment, "main_net_inflow", None)
        main_net_inflow = (
            float(persisted_main_inflow)
            if persisted_main_inflow is not None
            else None
        )

        return {
            "trade_date": as_of,
            "sh": sh,
            "sz": sz,
            "cyb": cyb,
            "sentiment": sentiment,
            "sentiment_state": sentiment_state,
            "limit_up_count_actual": int(limit_up_count),
            "limit_down_count_actual": int(limit_down_count),
            "main_net_inflow_actual": main_net_inflow,
        }

    @staticmethod
    def _theme_keyword_hit(sector_name: str, keywords: list[str]) -> bool:
        if not sector_name or not keywords:
            return False
        name = str(sector_name)
        name_upper = name.upper()
        for kw in keywords:
            if not kw:
                continue
            k = str(kw)
            if k in name or k.upper() in name_upper:
                return True
        return False

    def _expand_keywords(self, keywords: list[str]) -> list[str]:
        expanded: list[str] = []
        for kw in keywords or []:
            expanded.append(kw)
            expanded.extend(self.THEME_KEYWORD_EXPANSION.get(kw, []))
        # 去重但保持顺序
        return list(dict.fromkeys([x for x in expanded if x]))

    async def _load_theme_sector_rows(self, db: AsyncSession, as_of: date) -> tuple[date | None, list[dict]]:
        latest_date = (
            await db.execute(
                select(func.max(SectorPersistence.trade_date)).where(SectorPersistence.trade_date <= as_of)
            )
        ).scalar_one_or_none()
        if latest_date is None:
            latest_date = (await db.execute(select(func.max(SectorPersistence.trade_date)))).scalar_one_or_none()
        if latest_date is None:
            return None, []

        rows = (
            await db.execute(
                select(
                    SectorPersistence.sector_code,
                    SectorPersistence.sector_name,
                    SectorPersistence.change_pct,
                    SectorPersistence.fund_flow,
                    SectorPersistence.strength_score,
                    SectorPersistence.consecutive_days,
                    SectorPersistence.limit_up_count,
                    SectorInfo.is_excluded,
                )
                .select_from(SectorPersistence)
                .outerjoin(SectorInfo, SectorInfo.sector_code == SectorPersistence.sector_code)
                .where(
                    and_(
                        SectorPersistence.trade_date == latest_date,
                        (SectorInfo.is_excluded.is_(None) | (SectorInfo.is_excluded == 0)),
                    )
                )
            )
        ).all()

        sector_rows = []
        for r in rows:
            sector_rows.append(
                {
                    "sector_code": r[0],
                    "sector_name": r[1] or "",
                    "change_pct": float(r[2] or 0),
                    "fund_flow": float(r[3] or 0),
                    "strength_score": float(r[4] or 0),
                    "consecutive_days": int(r[5] or 0),
                    "limit_up_count": int(r[6] or 0),
                }
            )
        return latest_date, sector_rows

    def _build_theme_metrics(self, sector_rows: list[dict]) -> dict[str, dict]:
        metrics: dict[str, dict] = {}
        for source_key, cfg in GLOBAL_TO_CN_SECTOR_MAPPING.items():
            keywords = self._expand_keywords(cfg.get("a_share_themes", []))
            hits = [r for r in sector_rows if self._theme_keyword_hit(r["sector_name"], keywords)]
            if not hits:
                metrics[source_key] = {
                    "hit_count": 0,
                    "avg_change": 0.0,
                    "avg_fund": 0.0,
                    "active_ratio": 0.0,
                    "theme_signal": 0.0,
                    "top_themes": [],
                }
                continue

            n = len(hits)
            avg_change = sum(r["change_pct"] for r in hits) / n
            avg_fund = sum(r["fund_flow"] for r in hits) / n
            active_ratio = sum(1 for r in hits if r["consecutive_days"] >= 1 or r["limit_up_count"] > 0) / n
            # 组合信号：价格主导 + 资金辅助
            theme_signal = avg_change + avg_fund * 0.03
            top = sorted(
                hits,
                key=lambda x: abs(x["change_pct"]) + abs(x["fund_flow"]) * 0.03 + x["strength_score"] * 0.01,
                reverse=True,
            )[:3]
            metrics[source_key] = {
                "hit_count": n,
                "avg_change": round(avg_change, 2),
                "avg_fund": round(avg_fund, 2),
                "active_ratio": round(active_ratio, 2),
                "theme_signal": round(theme_signal, 2),
                "top_themes": [x["sector_name"] for x in top],
            }
        return metrics

    @staticmethod
    def _factor_to_a_share_impulse(source_key: str, change_pct: float) -> float:
        v = change_pct or 0.0
        # 美元走强/美债利率上行通常压制A股风险偏好和成长风格
        if source_key in {"fx_usdcny", "rates_us10y"}:
            return -v
        return v

    def _judge_mapping_status(
        self,
        source_key: str,
        factor_change: float,
        theme_metric: dict,
        theme_trade_date: date | None,
    ) -> tuple[str, str]:
        if not theme_metric or theme_metric.get("hit_count", 0) == 0:
            return "pending", "A股侧未匹配到可用主题板块，等待口径扩展"

        impulse = self._factor_to_a_share_impulse(source_key, factor_change)
        theme_signal = float(theme_metric.get("theme_signal") or 0.0)
        avg_change = float(theme_metric.get("avg_change") or 0.0)
        avg_fund = float(theme_metric.get("avg_fund") or 0.0)
        active_ratio = float(theme_metric.get("active_ratio") or 0.0)
        hit_count = int(theme_metric.get("hit_count") or 0)
        top_themes = theme_metric.get("top_themes") or []

        same_direction = (impulse >= 0 and theme_signal >= 0) or (impulse <= 0 and theme_signal <= 0)
        factor_strong = abs(impulse) >= 0.25
        theme_strong = abs(theme_signal) >= 0.35

        if not factor_strong and not theme_strong:
            status = "lagging"
        elif same_direction and theme_strong:
            status = "synced"
        elif same_direction:
            status = "lagging"
        else:
            status = "diverging"

        date_text = theme_trade_date.isoformat() if theme_trade_date else "--"
        top_text = "、".join(top_themes[:2]) if top_themes else "暂无代表板块"
        note = (
            f"A股主题样本{hit_count}个(口径{date_text})，均涨跌{avg_change:+.2f}%，"
            f"均资金{avg_fund:+.2f}亿，活跃占比{active_ratio * 100:.0f}%，代表:{top_text}"
        )
        return status, note

    def _sentiment_cycle_label(self, cycle: str | None) -> str:
        return self.SENTIMENT_CYCLE_LABELS.get(cycle or "pending", cycle or "待定")

    def _mapping_impulse(self, factor_map: dict, source_key: str) -> float:
        factor = factor_map.get(source_key)
        if factor is None:
            return 0.0
        return float(self._factor_to_a_share_impulse(source_key, factor.change_pct or 0) or 0.0)

    def _build_conclusions(self, factors: list, mappings: list, ctx: dict) -> list[OverviewConclusionItem]:
        factor_map = {f.key: f for f in factors}
        sentiment_state = ctx.get("sentiment_state")
        sentiment = ctx.get("sentiment")
        cycle = (
            getattr(sentiment_state, "phase", None)
            or getattr(sentiment, "sentiment_cycle", None)
            or "pending"
        )

        risk_score = 0
        for key in ["us_nasdaq", "us_sp500", "china_adr", "a50"]:
            item = factor_map.get(key)
            if not item:
                continue
            if (item.change_pct or 0) > 0:
                risk_score += 1
            elif (item.change_pct or 0) < 0:
                risk_score -= 1

        if risk_score >= 2:
            outer_value, outer_tone, outer_note = "外围偏多", "positive", "纳指/标普/中概/A50 整体偏强"
        elif risk_score <= -2:
            outer_value, outer_tone, outer_note = "外围偏空", "negative", "主要外部风险资产整体承压"
        else:
            outer_value, outer_tone, outer_note = "外围中性偏震荡", "neutral", "强弱分化，未形成单边共振"

        cycle_map = {
            "recovery": ("A股情绪修复", "positive"),
            "climax": ("A股情绪亢奋", "warning"),
            "divergence": ("A股情绪分歧", "warning"),
            "freezing": ("A股情绪偏冷", "negative"),
            "pending": ("A股情绪待定", "neutral"),
        }
        mood_value, mood_tone = cycle_map.get(cycle, (cycle, "neutral"))
        cycle_label = self._sentiment_cycle_label(cycle)

        synced = [m for m in mappings if m.status == "synced"]
        diverging = [m for m in mappings if m.status == "diverging"]
        lagging = [m for m in mappings if m.status == "lagging"]
        positive_synced = [m for m in synced if self._mapping_impulse(factor_map, m.source_key) > 0]
        negative_synced = [m for m in synced if self._mapping_impulse(factor_map, m.source_key) < 0]
        positive_lagging = [m for m in lagging if self._mapping_impulse(factor_map, m.source_key) > 0]

        strongest = max(
            positive_synced,
            key=lambda x: self._mapping_impulse(factor_map, x.source_key),
            default=None,
        )
        biggest_div = max(
            diverging,
            key=lambda x: abs(self._mapping_impulse(factor_map, x.source_key)),
            default=None,
        )
        weakest_sync = min(
            negative_synced,
            key=lambda x: self._mapping_impulse(factor_map, x.source_key),
            default=None,
        )

        if strongest:
            strong_factor = factor_map.get(strongest.source_key)
            strong_value = strongest.source_label
            strong_note = f"当前同步最强，外部涨跌幅 {strong_factor.change_pct:.2f}%"
            strong_tone = "positive"
        elif positive_lagging:
            strong_value = positive_lagging[0].source_label
            strong_note = "已有方向，但A股映射强度尚未完全跟随"
            strong_tone = "warning"
        else:
            strong_value = "暂无清晰主线"
            strong_note = "尚未形成明确同步映射"
            strong_tone = "neutral"

        if biggest_div:
            div_factor = factor_map.get(biggest_div.source_key)
            div_value = biggest_div.source_label
            div_note = f"当前最大背离，外部涨跌幅 {div_factor.change_pct:.2f}%"
            div_tone = "negative"
        elif weakest_sync:
            weak_factor = factor_map.get(weakest_sync.source_key)
            div_value = weakest_sync.source_label
            div_note = f"当前同步走弱最明显，外部涨跌幅 {weak_factor.change_pct:.2f}%"
            div_tone = "warning"
        else:
            div_value = "暂无显著背离"
            div_note = "当前主要映射未见明显反向冲突"
            div_tone = "neutral"

        return [
            OverviewConclusionItem(key="outer_bias", label="外围判断", value=outer_value, tone=outer_tone, note=outer_note),
            OverviewConclusionItem(key="a_share_mood", label="A股情绪", value=mood_value, tone=mood_tone, note=f"情绪周期: {cycle_label}"),
            OverviewConclusionItem(key="strongest_mapping", label="最强映射方向", value=strong_value, tone=strong_tone, note=strong_note),
            OverviewConclusionItem(key="biggest_divergence", label="最大背离点", value=div_value, tone=div_tone, note=div_note),
        ]

    def _build_focus_strips(self, conclusions: list[OverviewConclusionItem], mappings: list[MappingInsightItem]) -> list[FocusStripItem]:
        strongest = next((x for x in conclusions if x.key == "strongest_mapping"), None)
        divergence = next((x for x in conclusions if x.key == "biggest_divergence"), None)
        mood = next((x for x in conclusions if x.key == "a_share_mood"), None)

        opportunity_label = strongest.value if strongest and strongest.value != "暂无清晰主线" else "等待更清晰机会"
        opportunity_detail = strongest.note if strongest else "同步主线尚未形成"
        if mood and mood.value in {"A股情绪修复", "A股情绪亢奋"} and strongest and strongest.value != "暂无清晰主线":
            opportunity_detail = f"{mood.value}，{strongest.note}"

        risk_label = divergence.value if divergence and divergence.value != "暂无显著背离" else "暂无突出风险线"
        risk_detail = divergence.note if divergence else "主要映射暂未见明显反向冲突"

        return [
            FocusStripItem(kind="opportunity", label=opportunity_label, detail=opportunity_detail, tone="positive" if strongest and strongest.tone == "positive" else "neutral"),
            FocusStripItem(kind="risk", label=risk_label, detail=risk_detail, tone="negative" if divergence and divergence.value != "暂无显著背离" else "neutral"),
        ]

    def _build_a_share_core(self, ctx: dict) -> AShareCoreState:
        sh = ctx.get("sh")
        sz = ctx.get("sz")
        cyb = ctx.get("cyb")
        sentiment = ctx.get("sentiment")
        sentiment_state = ctx.get("sentiment_state")
        main_inflow = ctx.get("main_net_inflow_actual")
        if main_inflow is None:
            main_inflow = getattr(sentiment, "main_net_inflow", 0) or 0

        # V2.2融合: 大盘环境判断 — 基于三大指数涨跌+情绪周期+涨跌家数比
        market_env = self._judge_market_environment(sh, sz, cyb, sentiment_state, ctx)
        buy_threshold = {"strong": 8.5, "neutral": 9.0, "weak": 9.5}.get(market_env, 9.0)

        return AShareCoreState(
            indices=[
                AShareIndexItem(code="000001", label="上证指数", price=getattr(sh, "close", 0) or 0, change_pct=getattr(sh, "change_pct", 0) or 0),
                AShareIndexItem(code="399001", label="深证成指", price=getattr(sz, "close", 0) or 0, change_pct=getattr(sz, "change_pct", 0) or 0),
                AShareIndexItem(code="399006", label="创业板指", price=getattr(cyb, "close", 0) or 0, change_pct=getattr(cyb, "change_pct", 0) or 0),
            ],
            sentiment_cycle=getattr(sentiment_state, "phase", None) or getattr(sentiment, "sentiment_cycle", None),
            limit_up_count=ctx.get("limit_up_count_actual", 0) or 0,
            limit_down_count=ctx.get("limit_down_count_actual", 0) or 0,
            seal_rate=getattr(sentiment_state, "seal_rate", 0) or getattr(sentiment, "seal_rate", 0) or 0,
            board_height=getattr(sentiment_state, "board_height", 0) or getattr(sentiment, "board_height", 0) or 0,
            main_net_inflow=main_inflow,
            market_environment=market_env,
            buy_threshold=buy_threshold,
        )

    def _judge_market_environment(self, sh, sz, cyb, sentiment_state, ctx: dict) -> str:
        """V2.2大盘环境判断: strong/neutral/weak

        判定逻辑:
        1. 三大指数涨跌幅 — 2/3以上涨>0.5% = 强, 2/3以上跌>0.5% = 弱
        2. 情绪周期 — recovery/climax偏强, freezing偏弱, divergence中性
        3. 涨跌停家数比 — 涨停>跌停*3 = 强, 跌停>涨停*2 = 弱
        """
        # 维度1: 指数涨跌
        up_count = 0
        down_count = 0
        for idx in [sh, sz, cyb]:
            chg = getattr(idx, "change_pct", 0) or 0
            if chg > 0.5:
                up_count += 1
            elif chg < -0.5:
                down_count += 1

        # 维度2: 情绪周期
        cycle = getattr(sentiment_state, "phase", None) or "pending"
        cycle_strength = {"climax": 1, "recovery": 1, "divergence": 0, "freezing": -1, "pending": 0}.get(cycle, 0)

        # 维度3: 涨跌停比
        limit_up = ctx.get("limit_up_count_actual", 0) or 0
        limit_down = ctx.get("limit_down_count_actual", 0) or 0
        limit_strength = 0
        if limit_down > 0 and limit_up > limit_down * 3:
            limit_strength = 1
        elif limit_up > 0 and limit_down > limit_up * 2:
            limit_strength = -1

        # 综合判定
        score = (1 if up_count >= 2 else (-1 if down_count >= 2 else 0)) + cycle_strength + limit_strength
        if score >= 2:
            return "strong"
        elif score <= -2:
            return "weak"
        else:
            return "neutral"

    def _build_summary_text(self, conclusions: list[OverviewConclusionItem], mappings: list[MappingInsightItem]) -> str:
        outer = next((x for x in conclusions if x.key == "outer_bias"), None)
        mood = next((x for x in conclusions if x.key == "a_share_mood"), None)
        strongest = next((x for x in conclusions if x.key == "strongest_mapping"), None)
        divergence = next((x for x in conclusions if x.key == "biggest_divergence"), None)

        synced_count = sum(1 for m in mappings if m.status == "synced")
        diverging_count = sum(1 for m in mappings if m.status == "diverging")
        lagging_count = sum(1 for m in mappings if m.status == "lagging")

        parts = []
        if outer and mood:
            connector = "同时"
            if diverging_count >= 3 and synced_count <= 1:
                connector = "不过"
            elif synced_count >= 3 and diverging_count == 0:
                connector = "且"
            parts.append(f"{outer.value}，{connector}{mood.value}")

        if strongest and strongest.value not in {"暂无清晰主线", "暂无显著背离"}:
            if synced_count >= 2:
                parts.append(f"当前同步线索里，{strongest.value}相对更强")
            elif lagging_count >= 2:
                parts.append(f"{strongest.value}这条线已有方向，但A股跟随仍不算充分")
            else:
                parts.append(f"短线可以优先盯{strongest.value}")

        if divergence and divergence.value != "暂无显著背离":
            if diverging_count >= 3:
                parts.append(f"背离点偏多，尤其要留意{divergence.value}这条线")
            else:
                parts.append(f"同时留意{divergence.value}这条背离线索")
        elif synced_count >= 3:
            parts.append("整体联动比前面更顺，市场在尝试形成共振")
        elif lagging_count >= 3:
            parts.append("外部方向有了，但A股内部反馈还偏慢，先看是否继续确认")

        if not parts:
            parts.append("当前外部与A股联动信号仍偏中性，先观察进一步共振")

        return "，".join(parts) + "。"

    async def _collect_external_factors(self):
        """外部行情为同步 AkShare/requests 调用，必须移出事件循环。"""
        return await asyncio.to_thread(external_factor_collector.collect)

    async def build_snapshot(self, db: AsyncSession) -> Dashboard2Snapshot:
        factors = await self._collect_external_factors()
        factor_map = {f.key: f for f in factors}
        a_share_ctx = await self._load_a_share_context(db)
        theme_trade_date, theme_sector_rows = await self._load_theme_sector_rows(
            db,
            a_share_ctx.get("trade_date") or date.today(),
        )
        theme_metrics = self._build_theme_metrics(theme_sector_rows)

        mappings = []
        for k, v in GLOBAL_TO_CN_SECTOR_MAPPING.items():
            factor = factor_map.get(k)
            if factor is None:
                mappings.append(MappingInsightItem(
                    source_key=k,
                    source_label=v["label"],
                    a_share_themes=v["a_share_themes"],
                    status="missing",
                    note="数据源待接入",
                ))
                continue

            status, note = self._judge_mapping_status(
                k,
                factor.change_pct or 0,
                theme_metrics.get(k, {}),
                theme_trade_date,
            )
            mappings.append(MappingInsightItem(
                source_key=k,
                source_label=v["label"],
                a_share_themes=v["a_share_themes"],
                status=status,
                note=note,
            ))

        conclusions = self._build_conclusions(factors, mappings, a_share_ctx)
        focus_strips = self._build_focus_strips(conclusions, mappings)
        a_share_core = self._build_a_share_core(a_share_ctx)
        summary_text = self._build_summary_text(conclusions, mappings)

        return Dashboard2Snapshot(
            trade_date=str(a_share_ctx.get("trade_date")) if a_share_ctx.get("trade_date") else None,
            snapshot_time=datetime.now().isoformat(timespec="seconds"),
            summary_text=summary_text,
            conclusions=conclusions,
            focus_strips=focus_strips,
            a_share_core=a_share_core,
            external_factors=factors,
            mapping_insights=mappings,
        )


dashboard2_service = Dashboard2Service()
