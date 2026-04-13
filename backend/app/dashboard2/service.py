"""Dashboard 2.0 聚合服务"""

from datetime import datetime, date
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.stock import StockDaily, MarketSentiment
from .mapping import GLOBAL_TO_CN_SECTOR_MAPPING
from .schemas import Dashboard2Snapshot, MappingInsightItem
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

    async def build_snapshot(self, db: AsyncSession) -> Dashboard2Snapshot:
        factors = external_factor_collector.collect()
        factor_map = {f.key: f for f in factors}
        a_share_ctx = await self._load_a_share_context(db)

        mappings = []
        for k, v in GLOBAL_TO_CN_SECTOR_MAPPING.items():
            factor = factor_map.get(k)
            if factor is None:
                note = "数据源待接入"
                if k == "a50":
                    note = "A50 当前公开口径仍不稳定，暂保持缺失而不伪造"
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

        return Dashboard2Snapshot(
            trade_date=None,
            snapshot_time=datetime.now().isoformat(timespec="seconds"),
            external_factors=factors,
            mapping_insights=mappings,
        )


dashboard2_service = Dashboard2Service()
