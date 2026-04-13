"""Dashboard 2.0 聚合服务"""

from datetime import datetime, date
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.stock import StockDaily, MarketSentiment
from .mapping import GLOBAL_TO_CN_SECTOR_MAPPING
from .schemas import Dashboard2Snapshot, MappingInsightItem, OverviewConclusionItem
from .external_sources import external_factor_collector


class Dashboard2Service:
    async def _load_a_share_context(self, db: AsyncSession) -> dict:
        today = date.today()
        indices = (
            await db.execute(
                select(StockDaily).where(
                    StockDaily.trade_date == today,
                    StockDaily.code.in_(["000001", "399001", "399006"]),
                )
            )
        ).scalars().all()
        sentiment = (
            await db.execute(
                select(MarketSentiment).where(MarketSentiment.trade_date == today)
            )
        ).scalar_one_or_none()

        idx_map = {row.code: row for row in indices}
        return {
            "sh": idx_map.get("000001"),
            "sz": idx_map.get("399001"),
            "cyb": idx_map.get("399006"),
            "sentiment": sentiment,
        }

    def _judge_mapping_status(self, source_key: str, factor_change: float, ctx: dict) -> tuple[str, str]:
        sh = getattr(ctx.get("sh"), "change_pct", 0) or 0
        sz = getattr(ctx.get("sz"), "change_pct", 0) or 0
        cyb = getattr(ctx.get("cyb"), "change_pct", 0) or 0
        sentiment = ctx.get("sentiment")
        cycle = getattr(sentiment, "sentiment_cycle", None)

        if source_key == "a50":
            anchor = sh
        elif source_key in {"us_nasdaq", "rates_us10y", "us_ai_semiconductor"}:
            anchor = cyb
        elif source_key in {"china_adr", "us_ev_clean_energy"}:
            anchor = sz
        elif source_key in {"us_sp500", "fx_usdcny"}:
            anchor = (sh + sz) / 2
            if source_key == "fx_usdcny":
                factor_change = -factor_change
        elif source_key in {"gold", "oil"}:
            anchor = sz
        else:
            anchor = (sh + sz + cyb) / 3

        if abs(factor_change) < 0.15 and abs(anchor) < 0.3:
            return "lagging", "外部与A股都偏震荡，等待方向确认"

        same_direction = (factor_change >= 0 and anchor >= 0) or (factor_change <= 0 and anchor <= 0)
        diff = abs(factor_change - anchor)

        if same_direction and diff <= 1.2:
            note = "外部方向与A股映射方向基本同步"
            if cycle in {"climax", "recovery"}:
                note += "，情绪端有配合"
            return "synced", note

        if same_direction:
            return "lagging", "方向一致，但A股映射强度暂未完全跟上"

        return "diverging", "外部方向与A股映射方向相反，属于背离状态"

    def _build_conclusions(self, factors: list, mappings: list, ctx: dict) -> list[OverviewConclusionItem]:
        factor_map = {f.key: f for f in factors}
        sentiment = ctx.get("sentiment")
        cycle = getattr(sentiment, "sentiment_cycle", "pending") or "pending"

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

        synced = [m for m in mappings if m.status == "synced"]
        diverging = [m for m in mappings if m.status == "diverging"]
        lagging = [m for m in mappings if m.status == "lagging"]

        strongest = max(
            synced,
            key=lambda x: abs((factor_map.get(x.source_key).change_pct if factor_map.get(x.source_key) else 0) or 0),
            default=None,
        )
        biggest_div = max(
            diverging,
            key=lambda x: abs((factor_map.get(x.source_key).change_pct if factor_map.get(x.source_key) else 0) or 0),
            default=None,
        )

        if strongest:
            strong_factor = factor_map.get(strongest.source_key)
            strong_value = strongest.source_label
            strong_note = f"当前同步最强，外部涨跌幅 {strong_factor.change_pct:.2f}%"
            strong_tone = "positive" if (strong_factor.change_pct or 0) >= 0 else "warning"
        elif lagging:
            strong_value = lagging[0].source_label
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
        else:
            div_value = "暂无显著背离"
            div_note = "当前主要映射未见明显反向冲突"
            div_tone = "neutral"

        return [
            OverviewConclusionItem(key="outer_bias", label="外围判断", value=outer_value, tone=outer_tone, note=outer_note),
            OverviewConclusionItem(key="a_share_mood", label="A股情绪", value=mood_value, tone=mood_tone, note=f"情绪周期: {cycle}"),
            OverviewConclusionItem(key="strongest_mapping", label="最强映射方向", value=strong_value, tone=strong_tone, note=strong_note),
            OverviewConclusionItem(key="biggest_divergence", label="最大背离点", value=div_value, tone=div_tone, note=div_note),
        ]

    async def build_snapshot(self, db: AsyncSession) -> Dashboard2Snapshot:
        factors = external_factor_collector.collect()
        factor_map = {f.key: f for f in factors}
        a_share_ctx = await self._load_a_share_context(db)

        mappings = []
        for k, v in GLOBAL_TO_CN_SECTOR_MAPPING.items():
            factor = factor_map.get(k)
            if factor is None:
                note = "数据源待接入"
                mappings.append(MappingInsightItem(
                    source_key=k,
                    source_label=v["label"],
                    a_share_themes=v["a_share_themes"],
                    status="missing",
                    note=note,
                ))
                continue

            status, note = self._judge_mapping_status(k, factor.change_pct or 0, a_share_ctx)
            mappings.append(MappingInsightItem(
                source_key=k,
                source_label=v["label"],
                a_share_themes=v["a_share_themes"],
                status=status,
                note=note,
            ))

        conclusions = self._build_conclusions(factors, mappings, a_share_ctx)

        return Dashboard2Snapshot(
            trade_date=None,
            snapshot_time=datetime.now().isoformat(timespec="seconds"),
            conclusions=conclusions,
            external_factors=factors,
            mapping_insights=mappings,
        )


dashboard2_service = Dashboard2Service()
