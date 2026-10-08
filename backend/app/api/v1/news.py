"""新闻面 API — 新闻缓存+AI增强NLP+影响映射"""

from collections import defaultdict
import asyncio
from datetime import date, datetime, time, timedelta
import json
import re
from uuid import uuid4
from typing import Any, Optional

from fastapi import APIRouter, Depends, Query
from loguru import logger
from sqlalchemy import and_, desc, func, or_, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import async_session, get_db
from app.models.governance import TradeCalendarModel
from app.models.news import FinanceNews
from app.models.paper import PaperAccount, PaperPosition
from app.models.stock import StockKline, StockSectorMapping, StockSpot
from app.news.engine import news_engine
from app.news.sources.base import NewsItem
from app.risk.lockup import lockup_manager

router = APIRouter()

_CODE_RE = re.compile(r"(?<!\d)(?:[0368]\d{5})(?!\d)")
_EXCLUDED_SECTOR_KEYWORDS = ("融资融券", "沪股通", "深股通", "转融券", "标普", "富时", "MSCI", "证金持股")
_MAJOR_SCOPES = {"market", "sector", "stock"}
_HIGH_TRUST_SOURCES = {"cninfo": 1.25, "cls": 1.1, "sina": 1.0, "em": 0.95, "ths": 0.9, "global": 0.85}
_IMPACT_SCOPE_WEIGHT = {"market": 1.25, "sector": 1.1, "stock": 1.0, "global": 0.8}
_EVENT_IMPACT_WEIGHT = {"high": 1.2, "medium": 1.0, "low": 0.75}
_EVENT_DIRECTION_WEIGHT = {"bullish": 1, "bearish": -1, "neutral": 0}
_AMBIGUOUS_STOCK_NAMES = {"中国", "银行", "证券", "科技", "股份", "集团", "能源", "控股", "发展"}
_SOURCE_LAYERS = {
    "cls": ("domestic_news", "国内市场新闻"),
    "em": ("domestic_news", "国内市场新闻"),
    "ths": ("domestic_news", "国内市场新闻"),
    "sina": ("domestic_news", "国内市场新闻"),
    "cninfo": ("disclosure", "公告披露"),
    "global": ("global_market", "全球市场异动"),
}
_ANALYZED_STATUSES = {"analyzed", "fallback"}
_PENDING_STATUSES = {"raw", "failed"}
_ANALYSIS_STALE_MINUTES = 30
_NEWS_ANALYSIS_JOBS: dict[str, dict] = {}


async def _commit_with_retry(db: AsyncSession, label: str, attempts: int = 3, delay: float = 2.0) -> bool:
    for attempt in range(1, attempts + 1):
        try:
            await db.commit()
            return True
        except OperationalError as exc:
            await db.rollback()
            if attempt >= attempts:
                logger.warning(f"{label}提交失败，数据库正忙: {exc}")
                return False
            await asyncio.sleep(delay * attempt)
    return False


def _json_list(value: Any) -> list:
    if not value:
        return []
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, list) else []
    except (TypeError, ValueError):
        return []


def _unique_codes(values: list[Any]) -> list[str]:
    codes = set()
    for value in values:
        code = str(value or "").strip()
        if not code:
            continue
        match = _CODE_RE.search(code)
        if match:
            codes.add(match.group(0))
        elif code.isdigit() and len(code) <= 6:
            codes.add(code.zfill(6))
    return sorted(codes)


def _stock_name_alias_allowed(name: str) -> bool:
    clean = str(name or "").strip()
    if len(clean) < 3:
        return False
    if clean.upper().startswith("ST"):
        return False
    if clean in _AMBIGUOUS_STOCK_NAMES:
        return False
    return True


def _infer_codes(news: FinanceNews, name_to_codes: dict[str, set[str]] | None = None) -> list[str]:
    text = f"{news.title or ''} {news.content or ''}"
    codes = set(_unique_codes(_json_list(news.related_codes))) | set(_CODE_RE.findall(text))
    for name, mapped_codes in (name_to_codes or {}).items():
        if name and name in text:
            codes.update(mapped_codes)
    return sorted(codes)


def _stock_code_evidence(news: FinanceNews, name_to_codes: dict[str, set[str]] | None = None) -> dict:
    """区分直接个股证据和主题/模型推断，避免宽泛行业新闻污染个股方向."""
    text = f"{news.title or ''} {news.content or ''}"
    stored_codes = set(_unique_codes(_json_list(news.related_codes)))
    text_codes = set(_CODE_RE.findall(text))
    name_codes: set[str] = set()
    matched_names: dict[str, str] = {}
    for name, mapped_codes in (name_to_codes or {}).items():
        if name and name in text:
            name_codes.update(mapped_codes)
            for code in mapped_codes:
                matched_names[code] = name

    direct_codes = text_codes | name_codes
    if news.source == "cninfo":
        direct_codes |= stored_codes
        for code in stored_codes:
            matched_names.setdefault(code, "公告源代码")

    all_codes = stored_codes | direct_codes
    return {
        "all_codes": sorted(all_codes),
        "direct_codes": sorted(direct_codes),
        "inferred_codes": sorted(all_codes - direct_codes),
        "matched_names": matched_names,
    }


def _sentiment_score(sentiment: str | None) -> int:
    if sentiment == "bullish":
        return 1
    if sentiment == "bearish":
        return -1
    return 0


def _event_direction_score(events: list[dict], fallback_sentiment: str | None) -> int:
    scores = [
        _EVENT_DIRECTION_WEIGHT.get(str(event.get("direction") or "").lower(), 0)
        for event in events
        if isinstance(event, dict)
    ]
    directional = [score for score in scores if score]
    if directional:
        total = sum(directional)
        return 1 if total > 0 else -1 if total < 0 else 0
    return _sentiment_score(fallback_sentiment)


def _max_event_impact_weight(events: list[dict]) -> float:
    weights = [
        _EVENT_IMPACT_WEIGHT.get(str(event.get("impact") or "medium").lower(), 1.0)
        for event in events
        if isinstance(event, dict)
    ]
    return max(weights) if weights else 1.0


def _recency_weight(publish_time: datetime | None, now: datetime | None = None) -> float:
    if not publish_time:
        return 0.75
    now = now or datetime.now()
    age_hours = max(0.0, (now - publish_time).total_seconds() / 3600)
    if age_hours <= 24:
        return 1.0
    if age_hours <= 72:
        return 0.85
    if age_hours <= 168:
        return 0.65
    return 0.45


def _impact_score(news: FinanceNews, events: list[dict], confidence: float, sentiment: str) -> float:
    direction = _event_direction_score(events, sentiment)
    if direction == 0:
        return 0.0
    source_weight = _HIGH_TRUST_SOURCES.get(news.source or "", 0.8)
    scope_weight = _IMPACT_SCOPE_WEIGHT.get(news.impact_scope or "stock", 1.0)
    event_weight = _max_event_impact_weight(events)
    score = direction * confidence * source_weight * scope_weight * event_weight * _recency_weight(news.publish_time)
    return round(max(-2.0, min(2.0, score)), 4)


def _source_layer(source: str | None) -> tuple[str, str]:
    return _SOURCE_LAYERS.get(source or "", ("other", "其他来源"))


def _is_news_analyzed(news: FinanceNews) -> bool:
    status = getattr(news, "nlp_status", None)
    if status:
        return status in _ANALYZED_STATUSES
    return float(news.bull_bear_confidence or 0) > 0


def _analyzed_filter():
    return or_(
        FinanceNews.nlp_status.in_(tuple(_ANALYZED_STATUSES)),
        FinanceNews.nlp_status.is_(None) & (FinanceNews.bull_bear_confidence > 0),
    )


def _pending_filter():
    stale_analyzing_time = datetime.now() - timedelta(minutes=_ANALYSIS_STALE_MINUTES)
    return or_(
        FinanceNews.nlp_status.in_(tuple(_PENDING_STATUSES)),
        (FinanceNews.nlp_status == "analyzing") & (
            or_(
                FinanceNews.nlp_analyzed_at.is_(None),
                FinanceNews.nlp_analyzed_at < stale_analyzing_time,
            )
        ),
        FinanceNews.nlp_status.is_(None) & (
            or_(
                FinanceNews.bull_bear_confidence.is_(None),
                FinanceNews.bull_bear_confidence <= 0,
            )
        ),
    )


def _active_analyzing_filter():
    active_since = datetime.now() - timedelta(minutes=_ANALYSIS_STALE_MINUTES)
    return and_(
        FinanceNews.nlp_status == "analyzing",
        FinanceNews.nlp_analyzed_at.is_not(None),
        FinanceNews.nlp_analyzed_at >= active_since,
    )


def _major_reasons(
    sentiment: str,
    impact_scope: str,
    confidence: float,
    importance: int,
    source: str | None = None,
    event_count: int = 0,
) -> list[str]:
    reasons = []
    if sentiment in {"bullish", "bearish"}:
        reasons.append(f"方向:{'利好' if sentiment == 'bullish' else '利空'}")
    if impact_scope in _MAJOR_SCOPES:
        reasons.append(f"影响范围:{impact_scope}")
    if source in {"cninfo", "cls"}:
        reasons.append(f"高权重来源:{source}")
    if event_count:
        reasons.append(f"事件:{event_count}")
    if confidence >= 0.55:
        reasons.append(f"置信度:{confidence:.2f}")
    if importance >= 7:
        reasons.append(f"重要性:{importance}")
    return reasons


def _is_major_news(
    sentiment: str,
    impact_scope: str,
    confidence: float,
    importance: int,
    source: str | None = None,
    events: list[dict] | None = None,
) -> bool:
    events = events or []
    has_high_event = any(
        isinstance(event, dict)
        and event.get("direction") in {"bullish", "bearish"}
        and event.get("impact") in {"high", "medium"}
        for event in events
    )
    trusted_source = source in {"cninfo", "cls"}
    return (
        sentiment in {"bullish", "bearish"}
        and impact_scope in _MAJOR_SCOPES
        and (
            confidence >= 0.65
            or has_high_event
            or (trusted_source and confidence >= 0.55 and importance >= 7)
        )
    )


def _apply_major_filter(news_items: list[dict], major_only: bool) -> list[dict]:
    if not major_only:
        return news_items
    return [item for item in news_items if item.get("is_major")]


def _news_payload(
    news: FinanceNews,
    holding_codes: set[str] | None = None,
    name_to_codes: dict[str, set[str]] | None = None,
) -> dict:
    holding_codes = holding_codes or set()
    code_evidence = _stock_code_evidence(news, name_to_codes)
    related_codes = code_evidence["all_codes"]
    direct_related_codes = code_evidence["direct_codes"]
    inferred_related_codes = code_evidence["inferred_codes"]
    related_sectors = sorted({str(v) for v in _json_list(news.related_sectors) if v})
    confidence = float(news.bull_bear_confidence or 0)
    sentiment = news.sentiment or "neutral"
    impact_scope = news.impact_scope or "stock"
    importance = int(news.importance or 5)
    events = _json_list(news.events_json)
    analyzed = _is_news_analyzed(news)
    matched_holdings = sorted(set(direct_related_codes) & holding_codes)
    layer, layer_label = _source_layer(news.source)
    is_major = analyzed and _is_major_news(sentiment, impact_scope, confidence, importance, news.source, events)
    score = _impact_score(news, events, confidence, sentiment) if analyzed else 0.0
    return {
        "id": news.id,
        "source": news.source,
        "source_layer": layer,
        "source_layer_label": layer_label,
        "title": news.title,
        "content": (news.content or "")[:260],
        "url": news.url,
        "publish_time": news.publish_time.isoformat() if news.publish_time else None,
        "category": news.category,
        "sentiment": sentiment,
        "confidence": confidence,
        "bull_bear": news.bull_bear or "neutral",
        "bull_bear_confidence": confidence,
        "importance": importance,
        "is_analyzed": analyzed,
        "nlp_status": news.nlp_status or ("analyzed" if analyzed else "raw"),
        "nlp_status_label": {"analyzed": "AI 参与分析", "fallback": "规则降级", "raw": "待清洗", "analyzing": "清洗中", "failed": "失败待重试"}.get(news.nlp_status, "历史分析" if analyzed else "待清洗"),
        "is_major": is_major,
        "major_reasons": _major_reasons(sentiment, impact_scope, confidence, importance, news.source, len(events)) if analyzed else [],
        "summary": news.summary or (news.content or "")[:120],
        "events": events,
        "impact_scope": impact_scope,
        "direction": "bullish" if score > 0 else "bearish" if score < 0 else "neutral",
        "direction_score": score,
        "source_weight": _HIGH_TRUST_SOURCES.get(news.source or "", 0.8),
        "recency_weight": _recency_weight(news.publish_time),
        "related_codes": related_codes,
        "direct_related_codes": direct_related_codes,
        "inferred_related_codes": inferred_related_codes,
        "stock_attribution": {
            "level": "direct" if direct_related_codes else "theme" if related_sectors else "none",
            "direct_codes": direct_related_codes,
            "inferred_codes": inferred_related_codes,
            "matched_names": code_evidence["matched_names"],
            "reason": (
                "标题/正文/公告源直接提及"
                if direct_related_codes
                else "仅有板块或模型主题推断，不能作为核心个股证据"
                if related_sectors or inferred_related_codes
                else "无明确个股归因"
            ),
        },
        "related_sectors": related_sectors,
        "impact": {
            "stock_codes": direct_related_codes,
            "inferred_stock_codes": inferred_related_codes,
            "sectors": related_sectors,
            "holding_related": bool(matched_holdings),
            "holding_codes": matched_holdings,
            "score": score,
        },
    }


async def _cached_news(
    db: AsyncSession,
    limit: int = 50,
    source: str = "",
    sentiment: str = "",
    since: datetime | None = None,
    analyzed_only: bool = False,
) -> list[FinanceNews]:
    stmt = select(FinanceNews)
    if source:
        stmt = stmt.where(FinanceNews.source == source)
    if sentiment:
        stmt = stmt.where(FinanceNews.sentiment == sentiment)
    if since:
        stmt = stmt.where(FinanceNews.publish_time >= since)
    if analyzed_only:
        stmt = stmt.where(_analyzed_filter())
    stmt = stmt.order_by(desc(FinanceNews.publish_time), desc(FinanceNews.id)).limit(limit)
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def _news_analysis_status(
    db: AsyncSession,
    since: datetime | None = None,
    source: str = "",
) -> dict:
    base_filters = []
    if since:
        base_filters.append(FinanceNews.publish_time >= since)
    if source:
        base_filters.append(FinanceNews.source == source)

    total_stmt = select(func.count(FinanceNews.id))
    analyzed_stmt = select(func.count(FinanceNews.id)).where(_analyzed_filter())
    analyzing_stmt = select(func.count(FinanceNews.id)).where(_active_analyzing_filter())
    if base_filters:
        total_stmt = total_stmt.where(*base_filters)
        analyzed_stmt = analyzed_stmt.where(*base_filters)
        analyzing_stmt = analyzing_stmt.where(*base_filters)

    total = int((await db.execute(total_stmt)).scalar_one() or 0)
    analyzed = int((await db.execute(analyzed_stmt)).scalar_one() or 0)
    analyzing = int((await db.execute(analyzing_stmt)).scalar_one() or 0)
    pending = max(0, total - analyzed - analyzing)
    # analyzed_count 保持既有口径；新增来源明细，不把规则降级包装成 AI 成功。
    counts = dict((await db.execute(
        select(FinanceNews.nlp_status, func.count(FinanceNews.id))
        .where(*base_filters).group_by(FinanceNews.nlp_status)
    )).all())
    return {
        "fetched_count": total,
        "analyzed_count": analyzed,
        "ai_analyzed_count": int(counts.get("analyzed", 0)),
        "fallback_count": int(counts.get("fallback", 0)),
        "legacy_analyzed_count": max(0, analyzed - counts.get("analyzed", 0) - counts.get("fallback", 0)),
        "failed_count": int(counts.get("failed", 0)),
        "analyzing_count": analyzing,
        "pending_count": pending,
        "analysis_rate": round(analyzed / total, 4) if total else 0.0,
    }


async def _latest_trade_date(db: AsyncSession) -> date:
    today = date.today()
    latest = (
        await db.execute(
            select(func.max(TradeCalendarModel.trade_date)).where(
                TradeCalendarModel.trade_date <= today,
                TradeCalendarModel.is_trade_day.is_(True),
            )
        )
    ).scalar_one_or_none()
    if latest:
        return latest
    latest_kline = (
        await db.execute(select(func.max(StockKline.trade_date)).where(StockKline.trade_date <= today))
    ).scalar_one_or_none()
    return latest_kline or today


async def _news_decision_window(db: AsyncSession) -> dict:
    latest_trade_date = await _latest_trade_date(db)
    today = date.today()
    is_gap_window = latest_trade_date < today
    since = datetime.combine(latest_trade_date, time(15, 0)) if is_gap_window else None
    return {
        "latest_trade_date": latest_trade_date,
        "since": since,
        "is_gap_window": is_gap_window,
        "label": (
            f"{latest_trade_date.isoformat()} 15:00后消息面"
            if is_gap_window else "最新新闻流"
        ),
    }


def _news_item_from_row(row: FinanceNews) -> NewsItem:
    from app.news.engine import news_item_from_row
    return news_item_from_row(row)


async def _analyze_pending_window(
    db: AsyncSession,
    since: datetime | None = None,
    limit: int = 100,
    concurrency: int = 3,
    exclude_ids: set[int] | None = None,
) -> dict:
    limit = max(1, min(limit, 300))
    concurrency = max(1, min(concurrency, 6))
    stmt = select(FinanceNews).where(_pending_filter())
    if exclude_ids:
        stmt = stmt.where(FinanceNews.id.not_in(exclude_ids))
    if since:
        stmt = stmt.where(FinanceNews.publish_time >= since)
    stmt = stmt.order_by(desc(FinanceNews.publish_time), desc(FinanceNews.id)).limit(limit)
    rows = list((await db.execute(stmt)).scalars().all())
    if not rows:
        return {"requested": limit, "processed": 0}

    selected_ids = [row.id for row in rows]
    items = [_news_item_from_row(row) for row in rows]
    for row in rows:
        row.nlp_status = "analyzing"
        row.nlp_error = None
        row.nlp_analyzed_at = datetime.now()
    if not await _commit_with_retry(db, "新闻AI分析标记"):
        return {"requested": limit, "processed": 0, "failed": 0, "busy": True}

    results = await news_engine.process_and_store(
        items,
        db_session=db,
        reset_dedup=True,
        concurrency=concurrency,
    )
    remaining = list((
        await db.execute(
            select(FinanceNews).where(
                FinanceNews.id.in_(selected_ids),
                FinanceNews.nlp_status == "analyzing",
            )
        )
    ).scalars().all())
    for row in remaining:
        row.nlp_status = "failed"
        row.nlp_error = "NLP处理未返回结果"
    if remaining:
        await _commit_with_retry(db, "新闻AI分析失败标记")
    return {"requested": limit, "processed": len(results), "failed": len(remaining), "attempted_ids": selected_ids}


async def _run_news_analysis_job(
    job_id: str,
    since: datetime | None,
    target_limit: int,
    batch_size: int,
    concurrency: int,
):
    job = _NEWS_ANALYSIS_JOBS[job_id]
    processed = 0
    failed = 0
    busy = False
    attempted_ids: set[int] = set()  # 同一任务不反复重试最新失败行，避免旧新闻饥饿。
    try:
        async with async_session() as db:
            while processed + failed < target_limit:
                current_batch = min(batch_size, target_limit - processed - failed)
                job.update({
                    "status": "running",
                    "current_batch": current_batch,
                    "updated_at": datetime.now().isoformat(timespec="seconds"),
                })
                result = await _analyze_pending_window(
                    db,
                    since=since,
                    limit=current_batch,
                    concurrency=concurrency,
                    exclude_ids=attempted_ids,
                )
                attempted_ids.update(result.get("attempted_ids") or [])
                if result.get("busy"):
                    job.update({
                        "status": "busy",
                        "error": "数据库正忙，本批分析稍后重试",
                        "updated_at": datetime.now().isoformat(timespec="seconds"),
                    })
                    busy = True
                    break
                batch_processed = int(result.get("processed") or 0)
                batch_failed = int(result.get("failed") or 0)
                processed += batch_processed
                failed += batch_failed
                status = await _news_analysis_status(db, since)
                job.update({
                    "status": "running",
                    "processed": processed,
                    "failed": failed,
                    "analysis": status,
                    "updated_at": datetime.now().isoformat(timespec="seconds"),
                })
                if batch_processed + batch_failed == 0 or status.get("pending_count", 0) <= 0:
                    break
            if not busy:
                job.update({
                    "status": "completed",
                    "processed": processed,
                    "failed": failed,
                    "completed_at": datetime.now().isoformat(timespec="seconds"),
                })
    except asyncio.CancelledError:
        job.update({
            "status": "cancelled",
            "processed": processed,
            "failed": failed,
            "completed_at": datetime.now().isoformat(timespec="seconds"),
        })
    except Exception as exc:
        logger.exception(f"新闻窗口AI分析任务失败: {exc}")
        job.update({
            "status": "failed",
            "error": str(exc),
            "processed": processed,
            "failed": failed,
            "completed_at": datetime.now().isoformat(timespec="seconds"),
        })


async def _ensure_news_cache(
    db: AsyncSession,
    limit: int = 50,
    source: str = "",
    sentiment: str = "",
    refresh: bool = False,
    refresh_items: int = 2,
    raw_limit_per_source: int = 30,
    since: datetime | None = None,
) -> list[FinanceNews]:
    cached = await _cached_news(db, limit=limit, source=source, sentiment=sentiment, since=since)
    if cached and not refresh:
        return cached

    try:
        refresh_items = max(0, min(refresh_items, 50))
        fetch_limit = max(5, min(raw_limit_per_source, 200))
        items = await news_engine.fetch_all(limit_per_source=fetch_limit)
        if items:
            items = sorted(
                items,
                key=lambda item: item.publish_time or datetime.min,
                reverse=True,
            )
            if since:
                items = [item for item in items if (item.publish_time or datetime.min) >= since]
            await news_engine.cache_raw_items(db, items, reset_dedup=True)
            if refresh_items > 0:
                await news_engine.process_and_store(items[:refresh_items], db_session=db, reset_dedup=True, concurrency=1)
    except Exception as exc:
        logger.error(f"新闻刷新失败，使用缓存数据: {exc}")

    refreshed = await _cached_news(db, limit=limit, source=source, sentiment=sentiment, since=since)
    return refreshed or cached


async def _refresh_news_cache_task(refresh_items: int = 4, raw_limit_per_source: int = 30):
    refresh_items = max(1, min(refresh_items, 50))
    raw_limit_per_source = max(5, min(raw_limit_per_source, 200))
    try:
        items = await news_engine.fetch_all(limit_per_source=raw_limit_per_source)
        items = sorted(
            items,
            key=lambda item: item.publish_time or datetime.min,
            reverse=True,
        )
        if not items:
            return
        async with async_session() as db:
            await news_engine.cache_raw_items(db, items, reset_dedup=True)
            await news_engine.process_and_store(items[:refresh_items], db_session=db, reset_dedup=True, concurrency=1)
    except Exception as exc:
        logger.error(f"后台新闻刷新失败: {exc}")


async def _open_holding_codes(db: AsyncSession) -> tuple[set[str], list[PaperPosition]]:
    account = (
        await db.execute(
            select(PaperAccount)
            .where(PaperAccount.account_name == "default", PaperAccount.status == "active")
            .order_by(PaperAccount.id)
            .limit(1)
        )
    ).scalar_one_or_none()
    if not account:
        return set(), []

    result = await db.execute(
        select(PaperPosition)
        .where(PaperPosition.account_id == account.id, PaperPosition.is_closed.is_(False))
        .order_by(PaperPosition.buy_time)
    )
    positions = list(result.scalars().all())
    return {p.code for p in positions}, positions


async def _sector_map_for_codes(db: AsyncSession, codes: set[str]) -> dict[str, set[str]]:
    if not codes:
        return {}
    result = await db.execute(
        select(StockSectorMapping).where(StockSectorMapping.code.in_(list(codes)))
    )
    sector_map: dict[str, set[str]] = defaultdict(set)
    for row in result.scalars().all():
        if row.sector_name and not any(keyword in row.sector_name for keyword in _EXCLUDED_SECTOR_KEYWORDS):
            sector_map[row.code].add(row.sector_name)
    return sector_map


async def _stock_name_map(db: AsyncSession, codes: set[str]) -> dict[str, str]:
    if not codes:
        return {}
    result = await db.execute(select(StockSpot).where(StockSpot.code.in_(list(codes))))
    return {row.code: row.name for row in result.scalars().all() if row.name}


async def _stock_name_aliases(db: AsyncSession) -> dict[str, set[str]]:
    result = await db.execute(select(StockSpot.code, StockSpot.name).where(StockSpot.name.is_not(None)))
    aliases: dict[str, set[str]] = defaultdict(set)
    for code, name in result.all():
        clean_name = str(name or "").strip()
        if not _stock_name_alias_allowed(clean_name):
            continue
        aliases[clean_name].add(code)
    return aliases


def _impact_summary(bucket: dict, key: str, news: dict):
    item = bucket.setdefault(
        key,
        {
            "key": key,
            "news_count": 0,
            "bullish_count": 0,
            "bearish_count": 0,
            "neutral_count": 0,
            "net_score": 0.0,
            "latest_news": [],
        },
    )
    item["news_count"] += 1
    sentiment = news.get("sentiment") or "neutral"
    if sentiment == "bullish":
        item["bullish_count"] += 1
    elif sentiment == "bearish":
        item["bearish_count"] += 1
    else:
        item["neutral_count"] += 1
    item["net_score"] = round(item["net_score"] + news["impact"]["score"], 4)
    if len(item["latest_news"]) < 3:
        item["latest_news"].append({
            "title": news.get("title"),
            "source": news.get("source"),
            "sentiment": sentiment,
            "publish_time": news.get("publish_time"),
        })


def _counter_top(values: list[str], limit: int = 5) -> list[dict]:
    counter: dict[str, int] = defaultdict(int)
    for value in values:
        if value:
            counter[value] += 1
    return [
        {"name": name, "count": count}
        for name, count in sorted(counter.items(), key=lambda item: (-item[1], item[0]))[:limit]
    ]


def _news_window_summary(news_items: list[dict], analysis: dict, name_map: dict[str, str] | None = None) -> dict:
    """生成当前消息窗口投研摘要；方向判断只使用已精洗新闻."""
    name_map = name_map or {}
    analyzed = [item for item in news_items if item.get("is_analyzed")]
    fetched_count = int(analysis.get("fetched_count") or len(news_items))
    analyzed_count = int(analysis.get("analyzed_count") or len(analyzed))
    pending_count = int(analysis.get("pending_count") or 0)
    analyzing_count = int(analysis.get("analyzing_count") or 0)

    if not analyzed:
        return {
            "headline": "当前返回样本暂无可用于方向判断的已处理新闻。",
            "direction": "neutral",
            "direction_label": "等待新闻处理",
            "net_score": 0.0,
            "major_count": 0,
            "bullish_count": 0,
            "bearish_count": 0,
            "neutral_count": 0,
            "top_sectors": [],
            "top_codes": [],
            "core_bullish_stocks": [],
            "bullish_themes": [],
            "major_titles": [],
            "interpretation": [
                "当前返回样本还没有已处理新闻，不能据此判断利好/利空；未处理不代表中性。",
                "请先执行“分析待处理新闻”；每批有数量上限，结果会区分 AI 参与分析和规则降级。",
            ],
            "key_points": [
                f"已入库{fetched_count}条 / 已处理{analyzed_count}条（含规则降级）/ 待处理{pending_count}条",
                "未精洗新闻不参与利好利空、重大新闻和影响映射判断",
            ],
            "data_quality": {
                "fetched_count": fetched_count,
                "analyzed_count": analyzed_count,
                "pending_count": pending_count,
                "analyzing_count": analyzing_count,
            },
        }

    bullish_count = sum(1 for item in analyzed if item.get("sentiment") == "bullish")
    bearish_count = sum(1 for item in analyzed if item.get("sentiment") == "bearish")
    neutral_count = len(analyzed) - bullish_count - bearish_count
    major_news = [item for item in analyzed if item.get("is_major")]
    net_score = round(sum(float(item.get("direction_score") or 0) for item in analyzed), 4)
    if net_score > 0.35:
        direction, direction_label = "bullish", "偏利好"
    elif net_score < -0.35:
        direction, direction_label = "bearish", "偏利空"
    else:
        direction, direction_label = "neutral", "中性/分歧"

    top_sectors = _counter_top([
        sector
        for item in analyzed
        for sector in (item.get("related_sectors") or [])
    ])
    top_codes = _counter_top([
        code
        for item in analyzed
        for code in (item.get("direct_related_codes") or [])
    ])
    stock_scores: dict[str, dict] = {}
    theme_scores: dict[str, dict] = {}
    for item in analyzed:
        if item.get("sentiment") != "bullish":
            continue
        score = max(0.0, float(item.get("direction_score") or 0))
        if score <= 0:
            continue
        event_logic = next(
            (
                str(event.get("description") or event.get("title") or "").strip()
                for event in item.get("events") or []
                if isinstance(event, dict) and str(event.get("description") or event.get("title") or "").strip()
            ),
            "",
        )
        for sector in item.get("related_sectors") or []:
            row = theme_scores.setdefault(sector, {
                "name": sector,
                "score": 0.0,
                "news_count": 0,
                "major_count": 0,
                "reasons": [],
            })
            row["score"] += score
            row["news_count"] += 1
            row["major_count"] += 1 if item.get("is_major") else 0
            if len(row["reasons"]) < 3:
                row["reasons"].append({
                    "title": item.get("title"),
                    "source": item.get("source"),
                    "summary": item.get("summary"),
                    "publish_time": item.get("publish_time"),
                    "logic": f"{sector}与新闻主题相关，核心逻辑：{event_logic or item.get('summary') or item.get('title')}；仅作为板块/主题方向，不等同于直接利好某只股票。",
                })

        for code in item.get("direct_related_codes") or []:
            row = stock_scores.setdefault(code, {
                "code": code,
                "name": name_map.get(code, ""),
                "score": 0.0,
                "news_count": 0,
                "major_count": 0,
                "sectors": set(),
                "reasons": [],
            })
            row["score"] += score
            row["news_count"] += 1
            row["major_count"] += 1 if item.get("is_major") else 0
            row["sectors"].update(item.get("related_sectors") or [])
            if len(row["reasons"]) < 3:
                row["reasons"].append({
                    "title": item.get("title"),
                    "source": item.get("source"),
                    "score": item.get("direction_score"),
                    "summary": item.get("summary"),
                    "publish_time": item.get("publish_time"),
                    "logic": (
                        f"新闻直接提及{name_map.get(code, code)}，AI判定为利好；"
                        f"影响范围={item.get('impact_scope') or 'unknown'}，置信度={float(item.get('confidence') or 0):.2f}，"
                        f"核心逻辑：{event_logic or item.get('summary') or item.get('title')}。"
                    ),
                })
    core_bullish_stocks = []
    for row in sorted(
        stock_scores.values(),
        key=lambda item: (item["score"], item["major_count"], item["news_count"]),
        reverse=True,
    )[:8]:
        core_bullish_stocks.append({
            **row,
            "score": round(row["score"], 4),
            "sectors": sorted(row["sectors"]),
        })
    bullish_themes = []
    for row in sorted(
        theme_scores.values(),
        key=lambda item: (item["score"], item["major_count"], item["news_count"]),
        reverse=True,
    )[:6]:
        bullish_themes.append({
            **row,
            "score": round(row["score"], 4),
            "logic": "、".join(
                reason.get("summary") or reason.get("title") or ""
                for reason in row["reasons"][:2]
                if reason.get("summary") or reason.get("title")
            )[:180],
        })
    major_titles = [
        {
            "title": item.get("title"),
            "sentiment": item.get("sentiment"),
            "direction_score": item.get("direction_score"),
            "source": item.get("source"),
            "publish_time": item.get("publish_time"),
        }
        for item in sorted(major_news, key=lambda n: abs(float(n.get("direction_score") or 0)), reverse=True)[:3]
    ]

    lead_sectors = "、".join(item["name"] for item in top_sectors[:3]) if top_sectors else "暂无集中板块"
    lead_stocks = "、".join(
        f"{item['name'] or item['code']}({item['code']})" for item in core_bullish_stocks[:3]
    ) if core_bullish_stocks else "暂无直接证据确认的个股"
    lead_themes = "、".join(item["name"] for item in bullish_themes[:3]) if bullish_themes else lead_sectors
    interpretation = [
        f"方向解读：当前返回样本中已处理{len(analyzed)}条新闻，净分{net_score:+.2f}，整体判断为{direction_label}。该判断只来自已精洗新闻，未精洗样本不进入方向计算。",
        f"利好主线：板块/主题受益集中在{lead_themes}；核心利好个股只采用标题、正文或公告源直接证据，当前指向{lead_stocks}。",
        f"证据强度：重大新闻{len(major_news)}条，利好{bullish_count}条、利空{bearish_count}条、中性{neutral_count}条；若同一股票由多条已精洗利好共同指向，排序会优先靠前。",
    ]
    if not core_bullish_stocks and bullish_themes:
        interpretation.append("归因提示：当前更多是行业/主题层面的受益，暂不把模型推断出的相关股票包装成核心利好个股。")
    if pending_count or analyzing_count:
        interpretation.append(f"完整性提示：仍有{pending_count}条待分析、{analyzing_count}条分析中，当前结论会随AI精洗继续更新。")

    key_points = [
        f"样本已处理{len(analyzed)}条，重大新闻{len(major_news)}条，样本净分{net_score:+.2f}",
        f"利好{bullish_count}条 / 利空{bearish_count}条 / 中性{neutral_count}条",
    ]
    if top_sectors:
        key_points.append("高频板块：" + "、".join(item["name"] for item in top_sectors[:3]))
    if pending_count or analyzing_count:
        key_points.append(f"仍有{pending_count}条待分析、{analyzing_count}条分析中，摘要会随AI精洗更新")

    return {
        "headline": f"当前样本消息面{direction_label}，已处理新闻净分{net_score:+.2f}。",
        "direction": direction,
        "direction_label": direction_label,
        "net_score": net_score,
        "major_count": len(major_news),
        "bullish_count": bullish_count,
        "bearish_count": bearish_count,
        "neutral_count": neutral_count,
        "top_sectors": top_sectors,
        "top_codes": top_codes,
        "core_bullish_stocks": core_bullish_stocks,
        "bullish_themes": bullish_themes,
        "major_titles": major_titles,
        "interpretation": interpretation,
        "key_points": key_points,
        "data_quality": {
            "fetched_count": fetched_count,
            "analyzed_count": analyzed_count,
            "pending_count": pending_count,
            "analyzing_count": analyzing_count,
        },
    }


@router.get("/list")
async def news_list(
    source: str = Query("", description="新闻源过滤"),
    sentiment: str = Query("", description="情感过滤 bullish/bearish/neutral"),
    source_layer: str = Query("", description="新闻源层级 domestic_news/disclosure/global_market"),
    major_only: bool = Query(False, description="只返回重大新闻"),
    limit: int = Query(50, ge=1, le=200, description="返回数量"),
    refresh: bool = Query(False, description="强制刷新新闻源并入库"),
    refresh_items: int = Query(0, ge=0, le=50, description="单次刷新最多AI清洗条数；0表示仅入库不精洗"),
    raw_limit_per_source: int = Query(30, ge=5, le=200, description="单源原始新闻抓取上限"),
    db: AsyncSession = Depends(get_db),
):
    """新闻列表 — 缓存优先，返回统一NLP字段"""
    holding_codes, _ = await _open_holding_codes(db)
    name_to_codes = await _stock_name_aliases(db)
    window = await _news_decision_window(db)
    rows = await _ensure_news_cache(
        db,
        limit=max(limit * 3, 50),
        source=source,
        sentiment=sentiment,
        refresh=refresh,
        refresh_items=refresh_items,
        raw_limit_per_source=raw_limit_per_source,
        since=window["since"],
    )
    news_items = [_news_payload(row, holding_codes, name_to_codes) for row in rows]
    if source_layer:
        news_items = [item for item in news_items if item.get("source_layer") == source_layer]
    news_items = _apply_major_filter(news_items, major_only)[:limit]
    analysis = await _news_analysis_status(db, window["since"], source=source)
    summary_codes = {code for item in news_items for code in (item.get("direct_related_codes") or [])}
    summary_name_map = await _stock_name_map(db, summary_codes)
    return {
        "news": news_items,
        "summary": _news_window_summary(news_items, analysis, summary_name_map),
        "analysis": analysis,
        "cache": {
            "enabled": True,
            "refreshed": refresh,
            "refresh_pending": False,
            "count": len(news_items),
            "refresh_items": refresh_items if refresh else 0,
            "raw_limit_per_source": raw_limit_per_source if refresh else 0,
        },
        "window": {
            "latest_trade_date": window["latest_trade_date"].isoformat(),
            "since": window["since"].isoformat() if window["since"] else None,
            "is_gap_window": window["is_gap_window"],
            "label": window["label"],
        },
        "filters": {
            "major_only": major_only,
            "major_rule": "方向明确 + 影响范围在A股相关层级 + 高置信/中高事件/高权重来源共同确认",
            "source_layers": [{"value": key, "label": label} for key, label in sorted(set(_SOURCE_LAYERS.values()))],
        },
    }


@router.get("/bull-bear")
async def bull_bear_news(
    limit: int = Query(20, ge=1, le=100),
    major_only: bool = Query(True, description="默认只看重大利好利空"),
    refresh: bool = Query(False),
    db: AsyncSession = Depends(get_db),
):
    """利好利空分类 — 基于已入库NLP结果"""
    holding_codes, _ = await _open_holding_codes(db)
    name_to_codes = await _stock_name_aliases(db)
    window = await _news_decision_window(db)
    rows = await _ensure_news_cache(db, limit=max(limit * 8, 80), refresh=refresh, since=window["since"], refresh_items=20)
    payloads = [_news_payload(row, holding_codes, name_to_codes) for row in rows]
    payloads = [item for item in payloads if item.get("is_analyzed")]
    payloads = _apply_major_filter(payloads, major_only)
    bull = [n for n in payloads if n.get("sentiment") == "bullish"]
    bear = [n for n in payloads if n.get("sentiment") == "bearish"]
    return {
        "bull": bull[:limit],
        "bear": bear[:limit],
        "major_only": major_only,
        "window": {
            "latest_trade_date": window["latest_trade_date"].isoformat(),
            "since": window["since"].isoformat() if window["since"] else None,
            "is_gap_window": window["is_gap_window"],
            "label": window["label"],
        },
    }


@router.get("/events")
async def news_events(
    event_type: str = Query("", description="事件类型过滤"),
    major_only: bool = Query(False, description="只返回重大新闻的事件"),
    limit: int = Query(20, ge=1, le=100),
    refresh: bool = Query(False),
    db: AsyncSession = Depends(get_db),
):
    """事件提取 — 基于统一NLP结果"""
    name_to_codes = await _stock_name_aliases(db)
    window = await _news_decision_window(db)
    rows = await _ensure_news_cache(db, limit=max(limit * 4, 50), refresh=refresh, since=window["since"], refresh_items=20)
    events = []
    for row in rows:
        news = _news_payload(row, name_to_codes=name_to_codes)
        if not news.get("is_analyzed"):
            continue
        if major_only and not news.get("is_major"):
            continue
        for event in news.get("events", []):
            if event_type and event.get("type") != event_type:
                continue
            events.append({
                **event,
                "source_news": news["title"],
                "sentiment": news["sentiment"],
                "related_codes": news["related_codes"],
                "related_sectors": news["related_sectors"],
                "publish_time": news["publish_time"],
            })
    return {
        "events": events[:limit],
        "window": {
            "latest_trade_date": window["latest_trade_date"].isoformat(),
            "since": window["since"].isoformat() if window["since"] else None,
            "is_gap_window": window["is_gap_window"],
            "label": window["label"],
        },
    }


@router.get("/impact-map")
async def news_impact_map(
    limit: int = Query(120, ge=1, le=300),
    major_only: bool = Query(False, description="只用重大新闻做影响映射"),
    refresh: bool = Query(False),
    db: AsyncSession = Depends(get_db),
):
    """新闻影响映射到个股、板块与模拟盘持仓"""
    holding_codes, positions = await _open_holding_codes(db)
    # News refresh owns rollback on article failure, which expires all ORM rows.
    # Keep the original holding observation, not a later reload after refresh.
    holding_snapshot = [
        {"code": p.code, "name": p.name, "buy_price": p.buy_price,
         "current_price": p.current_price, "profit_pct": p.profit_pct}
        for p in positions
    ]
    name_to_codes = await _stock_name_aliases(db)
    window = await _news_decision_window(db)
    rows = await _ensure_news_cache(db, limit=limit, refresh=refresh, since=window["since"], refresh_items=0)
    news_items = [_news_payload(row, holding_codes, name_to_codes) for row in rows]
    news_items = [item for item in news_items if item.get("is_analyzed")]
    news_items = _apply_major_filter(news_items, major_only)

    all_codes = {code for news in news_items for code in news.get("direct_related_codes", [])}
    sector_map = await _sector_map_for_codes(db, all_codes)
    name_map = await _stock_name_map(db, all_codes | holding_codes)

    stocks: dict[str, dict] = {}
    sectors: dict[str, dict] = {}
    for news in news_items:
        related_codes = set(news.get("direct_related_codes", []))
        related_sectors = set(news["related_sectors"])
        for code in related_codes:
            _impact_summary(stocks, code, news)
            related_sectors.update(sector_map.get(code, set()))
        for sector in related_sectors:
            _impact_summary(sectors, sector, news)

    stock_rows = []
    for code, item in stocks.items():
        stock_rows.append({
            "code": code,
            "name": name_map.get(code, ""),
            **item,
        })
    sector_rows = [{"sector_name": key, **item} for key, item in sectors.items()]

    holding_rows = []
    for position in holding_snapshot:
        impact = stocks.get(position["code"], {
            "news_count": 0,
            "bullish_count": 0,
            "bearish_count": 0,
            "neutral_count": 0,
            "net_score": 0.0,
            "latest_news": [],
        })
        holding_rows.append({
            "code": position["code"],
            "name": position["name"] or name_map.get(position["code"], ""),
            "buy_price": position["buy_price"],
            "current_price": position["current_price"],
            "profit_pct": position["profit_pct"],
            **impact,
        })

    def sort_key(item: dict) -> tuple:
        return (abs(float(item.get("net_score") or 0)), item.get("news_count") or 0)

    return {
        "as_of": datetime.now().isoformat(timespec="seconds"),
        "window": {
            "latest_trade_date": window["latest_trade_date"].isoformat(),
            "since": window["since"].isoformat() if window["since"] else None,
            "is_gap_window": window["is_gap_window"],
            "label": window["label"],
        },
        "stocks": sorted(stock_rows, key=sort_key, reverse=True)[:50],
        "sectors": sorted(sector_rows, key=sort_key, reverse=True)[:50],
        "holdings": sorted(holding_rows, key=sort_key, reverse=True),
        "news_count": len(news_items),
        "major_only": major_only,
    }


@router.post("/analyze-window")
async def analyze_news_window(
    limit: int = Query(100, ge=1, le=300, description="最多分析当前窗口待精洗新闻条数"),
    batch_size: int = Query(20, ge=1, le=50, description="单批AI分析条数，避免长请求阻塞"),
    concurrency: int = Query(3, ge=1, le=6, description="AI精洗并发度，受全局AI限流保护"),
    db: AsyncSession = Depends(get_db),
):
    """启动当前消息窗口待精洗新闻分析任务."""
    window = await _news_decision_window(db)
    before = await _news_analysis_status(db, window["since"])
    job_id = uuid4().hex
    job = {
        "job_id": job_id,
        "status": "queued",
        "requested": limit,
        "batch_size": batch_size,
        "concurrency": concurrency,
        "processed": 0,
        "failed": 0,
        "before": before,
        "analysis": before,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "window": {
            "latest_trade_date": window["latest_trade_date"].isoformat(),
            "since": window["since"].isoformat() if window["since"] else None,
            "is_gap_window": window["is_gap_window"],
            "label": window["label"],
        },
    }
    _NEWS_ANALYSIS_JOBS[job_id] = job
    asyncio.create_task(_run_news_analysis_job(job_id, window["since"], limit, batch_size, concurrency))
    return job


@router.post("/refresh-and-analyze")
async def refresh_and_analyze_news_window(
    raw_limit_per_source: int = Query(200, ge=5, le=300, description="单源原始新闻抓取上限"),
    limit: int = Query(300, ge=1, le=300, description="最多分析当前窗口待精洗新闻条数"),
    batch_size: int = Query(30, ge=1, le=50, description="单批AI分析条数"),
    concurrency: int = Query(3, ge=1, le=6, description="AI精洗并发度"),
    db: AsyncSession = Depends(get_db),
):
    """手动刷新当前消息窗口，并在入库后立即启动AI精洗任务."""
    window = await _news_decision_window(db)
    fetch_limit = max(5, min(raw_limit_per_source, 300))
    fetched_count = 0
    saved_count = 0
    try:
        items = await news_engine.fetch_all(limit_per_source=fetch_limit)
        fetched_count = len(items)
        if window["since"]:
            items = [
                item for item in items
                if (item.publish_time or datetime.min) >= window["since"]
            ]
        items = sorted(items, key=lambda item: item.publish_time or datetime.min, reverse=True)
        if items:
            saved_count = await news_engine.cache_raw_items(db, items, reset_dedup=True)
    except Exception as exc:
        logger.error(f"新闻刷新并精洗启动失败: {exc}")

    before = await _news_analysis_status(db, window["since"])
    job_id = uuid4().hex
    job = {
        "job_id": job_id,
        "status": "queued",
        "requested": limit,
        "batch_size": batch_size,
        "concurrency": concurrency,
        "processed": 0,
        "failed": 0,
        "refresh": {
            "raw_limit_per_source": fetch_limit,
            "fetched_count": fetched_count,
            "saved_count": saved_count,
        },
        "before": before,
        "analysis": before,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "window": {
            "latest_trade_date": window["latest_trade_date"].isoformat(),
            "since": window["since"].isoformat() if window["since"] else None,
            "is_gap_window": window["is_gap_window"],
            "label": window["label"],
        },
    }
    _NEWS_ANALYSIS_JOBS[job_id] = job
    if before.get("pending_count", 0) > 0:
        asyncio.create_task(_run_news_analysis_job(job_id, window["since"], limit, batch_size, concurrency))
    else:
        job.update({
            "status": "completed",
            "completed_at": datetime.now().isoformat(timespec="seconds"),
        })
    return job


@router.get("/analysis-jobs/{job_id}")
async def news_analysis_job_status(job_id: str):
    """查询新闻AI分析任务进度."""
    job = _NEWS_ANALYSIS_JOBS.get(job_id)
    if not job:
        return {"job_id": job_id, "status": "not_found"}
    return job


@router.get("/lockup-calendar")
async def lockup_calendar(
    days: int = Query(30, ge=1, le=180, description="查询天数"),
    risk_level: Optional[str] = Query(None, description="风险等级 high/medium/low"),
    refresh: bool = Query(False, description="为空或手动刷新时采集真实解禁数据"),
    db: AsyncSession = Depends(get_db),
):
    """解禁日历 — 接入真实解禁数据管理器"""
    lockups = await lockup_manager.get_upcoming_lockups(db, days=days, risk_level=risk_level)
    if refresh or not lockups:
        try:
            df = await lockup_manager.collect_lockup_data(
                db,
                start_date=date.today().strftime("%Y%m%d"),
                end_date=(date.today() + timedelta(days=days)).strftime("%Y%m%d"),
            )
            if df is not None and not df.empty:
                await lockup_manager.save_lockup_data(db, df)
                lockups = await lockup_manager.get_upcoming_lockups(db, days=days, risk_level=risk_level)
        except Exception as exc:
            logger.error(f"解禁数据刷新失败: {exc}")
    return {"lockup": lockups}


@router.get("/{code}")
async def stock_news(
    code: str,
    limit: int = Query(20, ge=1, le=100),
    refresh: bool = Query(False),
    db: AsyncSession = Depends(get_db),
):
    """个股新闻 — 优先从已入库NLP缓存按代码匹配"""
    holding_codes, _ = await _open_holding_codes(db)
    name_to_codes = await _stock_name_aliases(db)
    rows = await _ensure_news_cache(db, limit=max(limit * 5, 50), refresh=refresh)
    matched = [row for row in rows if code in _infer_codes(row, name_to_codes)]
    if not matched and refresh:
        try:
            items = await news_engine.fetch_by_code(code, limit=limit)
            if items:
                await news_engine.process_and_store(items, db_session=db, reset_dedup=True, concurrency=1)
                rows = await _cached_news(db, limit=max(limit * 5, 50))
                matched = [row for row in rows if code in _infer_codes(row, name_to_codes)]
        except Exception as exc:
            logger.error(f"个股新闻刷新失败: {exc}")
    return {
        "code": code,
        "news": [_news_payload(row, holding_codes, name_to_codes) for row in matched[:limit]],
    }
