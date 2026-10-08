"""新闻调度引擎 — 统一管理6源抓取+NLP+去重+入库

流程:
1. 调度6个新闻源抓取
2. 去重
3. NLP处理(AI增强)
4. 入库
5. 触发推送(重大新闻)
"""

import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from copy import copy
import hashlib
import json
import math
from loguru import logger
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from app.config.settings import settings
from app.news.sources.base import NewsItem
from app.news.sources.cls import ClsSource
from app.news.sources.em import EmSource
from app.news.sources.cninfo import CninfoSource
from app.news.sources.ths import ThsSource
from app.news.sources.sina import SinaSource
from app.news.sources.global_market import GlobalMarketSource
from app.news.nlp.processor import news_processor
from app.news.dedup import news_dedup
from app.news.dedup import content_hash
from app.models.news import FinanceNews, NewsContentVersion, NewsAnalysisVersion
from app.news.catalyst import NEWS_EVIDENCE_PROTOCOL, verify_news_entities
from app.news.roles import guard_news_analysis


def _news_now():
    return datetime.now(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)


def _local_time(value):
    if value is None:
        return None
    return value.astimezone(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None) if value.tzinfo else value


def _raw_payload(item):
    return {
        "source": item.source,
        "title": item.title or "", "content": item.content or "", "url": item.url or "",
        "publish_time": _local_time(item.publish_time).isoformat() if item.publish_time else None,
    }


def _encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _raw_hash(item):
    return hashlib.sha256(_encode(_raw_payload(item)).encode()).hexdigest()


def _analysis_status(result):
    """AI participation requires an explicit successful NLP method, not a fallback label."""
    return "analyzed" if any(result.get(key) == "ai" for key in
                             ("sentiment_method", "events_method")) else "fallback"



def news_item_from_row(row: FinanceNews) -> NewsItem:
    """Reconstruct stored text, never invent a historical receipt clock."""
    def sequence(raw):
        try:
            values = json.loads(raw or "[]")
            return values if isinstance(values, list) else []
        except (ValueError, TypeError):
            return []
    return NewsItem(source=row.source, title=row.title, content=row.content or "",
                    url=row.url or "", publish_time=row.publish_time,
                    source_id=row.source_id or "", category=row.category or "",
                    related_codes=sequence(row.related_codes), related_sectors=sequence(row.related_sectors))


class NewsEngine:
    """新闻调度引擎"""

    def __init__(self):
        # A timed-out requests/AkShare thread cannot be killed by task cancellation.
        # Keep one in-flight operation per source and reuse it, rather than spawning
        # another thread on every scheduler retry.
        self._fetch_tasks: dict[str, asyncio.Task] = {}
        self._fetch_health: dict[str, dict] = {}
        self.sources = {
            "cls": ClsSource(),
            "em": EmSource(),
            "cninfo": CninfoSource(),
            "ths": ThsSource(),
            "sina": SinaSource(),
            "global": GlobalMarketSource(),
        }

    def get_fetch_health(self) -> dict[str, dict]:
        """Transport observations are separate from article publication times.

        Empty results remain unverified: several adapters return [] on errors,
        so an empty response must never certify a source as healthy.
        """
        return {code: dict(observation) for code, observation in self._fetch_health.items()}

    def cancel_pending_fetches(self) -> None:
        """Release task handles on shutdown; underlying network calls need their own timeouts."""
        for task in tuple(self._fetch_tasks.values()):
            if not task.done():
                task.cancel()
        self._fetch_tasks.clear()

    async def fetch_all(self, limit_per_source: int = 30) -> list[NewsItem]:
        """抓取各源已完成的结果；单个慢源有界等待，不拖住其余来源。"""
        async def fetch_one(code: str, source) -> list[NewsItem]:
            task = self._fetch_tasks.get(code)
            if task is None:
                # Legacy adapters contain blocking I/O, including nested to_thread.
                async def fetch_received():
                    items = await source.fetch_latest(limit=limit_per_source)
                    # Stamp at adapter completion, not the later poll consuming
                    # this task (which may have timed out on the first poll).
                    received_at = _news_now()
                    if isinstance(items, list):
                        for item in items:
                            if isinstance(item, NewsItem):
                                item._news_received_at = received_at
                    return items

                task = asyncio.create_task(asyncio.to_thread(
                    lambda: asyncio.run(fetch_received())
                ))
                # Retrieve late errors even if no subsequent fetch consumes them.
                task.add_done_callback(lambda done: None if done.cancelled() else done.exception())
                self._fetch_tasks[code] = task
            try:
                items = await asyncio.wait_for(
                    asyncio.shield(task),
                    timeout=max(0.01, float(settings.NEWS_FETCH_SOURCE_TIMEOUT_SEC)),
                )
                if not isinstance(items, list) or any(not isinstance(item, NewsItem) for item in items):
                    raise ValueError("news source returned an invalid payload")
                self._fetch_health[code] = {
                    "status": "ok" if items else "no_data",
                    "observed_at": datetime.now(),
                    "item_count": len(items),
                }
                if code == "global" and isinstance(getattr(source, "last_observation", None), dict):
                    self._fetch_health[code]["index_target_observation"] = dict(source.last_observation)
                logger.info(f"新闻源 [{source.source_name}]: 获取{len(items)}条")
                return items
            except asyncio.TimeoutError:
                self._fetch_health[code] = {"status": "timeout", "observed_at": datetime.now()}
                logger.warning(f"新闻源 [{source.source_name}] 超出时间预算；保留单一在途请求")
                return []
            except Exception as e:
                self._fetch_health[code] = {"status": "failed", "observed_at": datetime.now()}
                logger.error(f"新闻源 [{source.source_name}] 抓取异常: {e}")
                return []
            finally:
                if task.done() and self._fetch_tasks.get(code) is task:
                    self._fetch_tasks.pop(code, None)

        results = await asyncio.gather(
            *(fetch_one(code, source) for code, source in self.sources.items())
        )
        return [item for items in results for item in items]

    async def fetch_by_code(self, code: str, limit: int = 20) -> list[NewsItem]:
        """获取个股相关新闻(所有源)"""
        all_items = []
        for source in self.sources.values():
            try:
                items = await source.fetch_by_code(code, limit=limit)
                for item in items:
                    item._news_received_at = _news_now()
                all_items.extend(items)
            except Exception as e:
                logger.error(f"个股新闻 [{source.source_name}] 异常: {e}")
        return all_items


    async def analyze_pending(self, db, *, limit=80, since=None, concurrency=5):
        """Enrich persisted raw text; scheduler does not refetch to start NLP."""
        from sqlalchemy import and_, func, or_
        from app.ai.provider import ai_provider
        if type(limit) is not int or not 1 <= limit <= 300:
            raise ValueError("news analysis batch must be within 1..300")
        now = _news_now()
        cooled = or_(FinanceNews.nlp_analyzed_at.is_(None),
                     FinanceNews.nlp_analyzed_at <= now - timedelta(seconds=settings.NEWS_AI_RETRY_COOLDOWN_SEC))
        pending = [and_(or_(FinanceNews.nlp_status.in_(["raw", "failed"]),
                            FinanceNews.nlp_status.is_(None)), cooled)]
        # Payment recovery can enrich old keyword results forward in time, but
        # never retry the same fallback on every recovery tick while blocked.
        if ai_provider.enabled and not ai_provider.runtime_status()["circuit_open"]:
            mixed = and_(FinanceNews.nlp_status == "analyzed", or_(
                FinanceNews.sentiment_method.is_(None), FinanceNews.sentiment_method != "ai",
                FinanceNews.events_method.is_(None), FinanceNews.events_method != "ai"))
            pending.append(and_(or_(FinanceNews.nlp_status == "fallback", mixed),
                or_(FinanceNews.nlp_analyzed_at.is_(None),
                    FinanceNews.nlp_analyzed_at <= now - timedelta(seconds=settings.NEWS_AI_FALLBACK_RETRY_SEC))))
        criteria = [or_(*pending), FinanceNews.source != "global"]
        if since is not None:
            criteria.append(FinanceNews.publish_time >= since)
        total = await db.scalar(select(func.count()).select_from(FinanceNews).where(*criteria))
        rows = list((await db.scalars(select(FinanceNews).where(*criteria)
                   .order_by(FinanceNews.publish_time.desc(), FinanceNews.id.desc()).limit(limit))).all())
        # Commit small waves through the existing owner. A healthy slow model
        # must not lose every completed result when the overall job times out.
        results = []
        wave = settings.NEWS_AI_WAVE_SIZE
        for start in range(0, len(rows), wave):
            # Durable attempt watermarks prevent a persistently failing newest
            # prefix from monopolizing the next bounded job. This projection
            # clock is NOT an immutable successful-analysis availability clock.
            for row in rows[start:start + wave]:
                row.nlp_analyzed_at = _news_now()
            await db.commit()
            results.extend(await self.process_and_store(
                [news_item_from_row(row) for row in rows[start:start + wave]],
                db_session=db, reset_dedup=True, concurrency=concurrency))
        counts = {"ai_full": 0, "ai_partial": 0, "keyword": 0}
        for result in results:
            methods = [result.get(key) == "ai" for key in ("sentiment_method", "events_method")]
            counts["ai_full" if all(methods) else "ai_partial" if any(methods) else "keyword"] += 1
        return {"status": "no_pending" if not rows else (
                    "analyzed" if counts["ai_full"] == len(rows) and total == len(rows) else "partial"),
                "pending_at_start": total, "pending_scope": "eligible_after_retry_cooldown",
                "selected": len(rows), "processed": len(results),
                "failed": len(rows) - len(results), **counts, "selection_truncated": total > len(rows)}

    async def process_and_store(
        self,
        items: list[NewsItem],
        db_session=None,
        reset_dedup: bool = True,
        concurrency: int = 5,
    ) -> list[dict]:
        """处理并存储新闻

        流程: 去重 → NLP → 入库 → 返回结果
        """
        # 1. 去重
        if reset_dedup:
            news_dedup.reset()
        unique = self._exact_items(items)
        if not unique:
            return []

        # Persist inputs before any NLP awaits. Reconstructed legacy page rows
        # are not fresh receipts; _capture_content refuses to certify them.
        if db_session is not None:
            for item in unique:
                version = await self._save_raw_to_db(db_session, item, observed=False)
                item._news_content_version_id = version.id
                item._news_entity_evidence = json.loads(version.entity_evidence_json)
                # This batch owns its commits. Release the writer after each
                # complete article, not after all entity queries in the batch.
                # All inputs are still durable before the first NLP await.
                await db_session.commit()

        # 2. NLP处理
        semaphore = asyncio.Semaphore(max(1, min(concurrency, 10)))

        async def process_one(item: NewsItem) -> tuple[NewsItem, dict | None]:
            try:
                async with semaphore:
                    nlp_result = await news_processor.process(item)
                    item._news_analysis_completed_at = _news_now()
                    if not isinstance(nlp_result, dict) or not nlp_result:
                        raise ValueError("empty news analysis")
                result = {
                    "source": item.source,
                    "title": item.title,
                    "content": item.content,
                    "url": item.url,
                    "publish_time": item.publish_time.isoformat() if item.publish_time else None,
                    "source_id": self._source_id(item),
                    **nlp_result,
                }
                return item, result
            except Exception as e:
                item._news_analysis_completed_at = _news_now()
                logger.error(f"新闻处理异常: {type(e).__name__}")
                return item, None

        processed = await asyncio.gather(*(process_one(item) for item in unique))
        results = []
        for item, result in processed:
            if not result:
                if db_session is not None:
                    await self._append_analysis(db_session, item, {}, status="failed")
                    await db_session.commit()
                continue
            if db_session:
                saved = await self._save_to_db_with_retry(db_session, item, result)
                if not saved:
                    continue
            results.append(result)

        logger.info(f"新闻处理完成: {len(unique)}条入库, {len(results)}条成功")
        return results

    async def _save_to_db_with_retry(self, session, item: NewsItem, result: dict, attempts: int = 3) -> bool:
        for attempt in range(1, attempts + 1):
            try:
                await self._save_to_db(session, item, result)
                await session.commit()
                return True
            except (ValueError, TypeError, OverflowError) as exc:
                await session.rollback()
                await self._append_analysis(session, item, {}, status="failed")
                await session.commit()
                logger.warning(f"新闻精洗结果无效: {type(exc).__name__}")
                return False
            except OperationalError as exc:
                await session.rollback()
                if attempt >= attempts:
                    logger.warning(f"新闻精洗入库失败，数据库正忙: {item.title} {exc}")
                    return False
                await asyncio.sleep(2 * attempt)
        return False

    async def cache_raw_items(self, session, items: list[NewsItem], reset_dedup: bool = True) -> int:
        """快速缓存原始新闻，AI结果后续再覆盖更新."""
        if reset_dedup:
            news_dedup.reset()
        unique = self._exact_items(items)
        if not unique:
            return 0

        saved = 0
        for item in unique:
            try:
                # A standalone SQLite SAVEPOINT may commit on RELEASE before
                # session.commit(). Use the actual per-article owner transaction.
                await self._save_raw_to_db(session, item)
                # Do not retain an outer snapshot across independent articles.
                # This owner already committed the batch; now commit per item.
                await session.commit()
                saved += 1
            except Exception as e:
                # ROLLBACK TO SAVEPOINT does not release a stale WAL snapshot.
                # The owning batch must end it before processing the next item.
                # Earlier successful articles have already been committed.
                await session.rollback()
                logger.error(f"原始新闻缓存异常: {e}")

        logger.info(f"原始新闻缓存完成: {saved}/{len(unique)}条")
        return saved

    def _source_id(self, item: NewsItem) -> str:
        if item.source_id:
            return item.source_id
        digest_text = f"{item.title or ''} {item.content or ''}".strip() or (item.url or "")
        return content_hash(digest_text or item.source)

    def _exact_items(self, items):
        # Similarity/source-id dedup would silently erase content revisions.
        # Only adjacent identical observations collapse (A->B->A is retained).
        unique = []
        last = {}
        for item in items:
            key = (item.source, self._source_id(item))
            digest = _raw_hash(item)
            if last.get(key) != digest:
                unique.append(copy(item))
                last[key] = digest
        return unique

    async def _capture_content(self, session, item, *, observed):
        existing = (await session.execute(select(FinanceNews).where(
            FinanceNews.source == item.source, FinanceNews.source_id == self._source_id(item),
        ))).scalar_one_or_none()
        is_new = existing is None
        if is_new:
            existing = FinanceNews(
                source=item.source, source_id=self._source_id(item), title=item.title,
                content=item.content, url=item.url, publish_time=_local_time(item.publish_time) or _news_now(),
                crawl_time=_news_now(), nlp_status="raw",
            )
            session.add(existing)
            await session.flush()
        latest = (await session.execute(select(NewsContentVersion).where(
            NewsContentVersion.news_id == existing.id,
        ).order_by(NewsContentVersion.id.desc()).limit(1))).scalar_one_or_none()
        digest = _raw_hash(item)
        if (latest is not None and latest.content_hash == digest
                and (latest.origin == "observed" or not (observed or getattr(item, "_news_received_at", None)))):
            return latest
        # process_and_store can be called on rows reconstructed from FinanceNews.
        # Only a real ingest or a newly received item is a forward observation.
        fresh = observed or is_new or getattr(item, "_news_received_at", None) is not None
        received = _local_time(getattr(item, "_news_received_at", None)) or _news_now()
        if not fresh and latest is not None:
            previous = (await session.execute(select(NewsContentVersion).where(
                NewsContentVersion.news_id == existing.id,
                NewsContentVersion.content_hash == digest,
            ).order_by(NewsContentVersion.id.desc()).limit(1))).scalar_one_or_none()
            if previous is not None:
                return previous
        evidence = await verify_news_entities(session, item.title, item.content)
        now = _news_now()
        first_received = latest.first_received_at if latest is not None else received if is_new else None
        version = NewsContentVersion(
            news_id=existing.id, source=item.source, content_hash=digest,
            publish_time=_local_time(item.publish_time), first_received_at=first_received,
            received_at=received if fresh else None,
            content_available_at=now if fresh else None, recorded_at=now,
            origin="observed" if fresh else "legacy_unknown",
            payload_json=_encode(_raw_payload(item)),
            entity_evidence_json=_encode(evidence), entity_verified_at=now,
            protocol_version=NEWS_EVIDENCE_PROTOCOL,
        )
        session.add(version)
        await session.flush()
        return version

    async def _append_analysis(self, session, item, result, *, status=None):
        version_id = getattr(item, "_news_content_version_id", None)
        version = await session.get(NewsContentVersion, version_id) if version_id else None
        if version is None:
            version = await self._capture_content(session, item, observed=False)
        if version.content_hash != _raw_hash(item):
            raise ValueError("news analysis input revision mismatch")
        now = _news_now()
        completed = _local_time(getattr(item, "_news_analysis_completed_at", None)) or now
        if completed > now or completed < version.recorded_at:
            raise ValueError("invalid news analysis completion clock")
        if status != "failed":
            confidence = result.get("confidence", 0)
            if (not isinstance(confidence, (int, float)) or isinstance(confidence, bool)
                    or not math.isfinite(confidence) or not 0 <= confidence <= 1
                    or result.get("sentiment") not in {"bullish", "bearish", "neutral", "positive", "negative"}
                    or not isinstance(result.get("events", []), list)
                    or not all(isinstance(e, dict) for e in result.get("events", []))
                    or not isinstance(result.get("related_sectors", []), list)
                    or not all(isinstance(s, str) for s in result.get("related_sectors", []))):
                raise ValueError("invalid news analysis result")
            result = {**result, "importance": self._calc_importance(result)}
        encoded = _encode(result)
        result_hash = hashlib.sha256(encoded.encode()).hexdigest()
        result_status = status or _analysis_status(result)
        previous = (await session.execute(select(NewsAnalysisVersion).where(
            NewsAnalysisVersion.content_version_id == version.id,
        ).order_by(NewsAnalysisVersion.available_at.desc(), NewsAnalysisVersion.id.desc()).limit(1))).scalar_one_or_none()
        if (result_status != "failed" and previous is not None
                and previous.result_hash == result_hash and previous.status == result_status):
            # A repeated identical successful poll is not new information.
            # Preserve the first completion/availability of this consecutive result.
            return version
        attempt = NewsAnalysisVersion(
            content_version_id=version.id,
            status=result_status,
            analysis_completed_at=completed, available_at=now, result_json=encoded,
            result_hash=result_hash,
            protocol_version=NEWS_EVIDENCE_PROTOCOL,
        )
        session.add(attempt)
        await session.flush()
        return version

    async def _save_raw_to_db(self, session, item: NewsItem, *, observed=True):
        """Append raw evidence, then update only the compatible page projection."""
        version = await self._capture_content(session, item, observed=observed)
        latest_id = await session.scalar(select(NewsContentVersion.id).where(
            NewsContentVersion.news_id == version.news_id,
        ).order_by(NewsContentVersion.id.desc()).limit(1))
        if latest_id != version.id:
            return version
        from app.models.news import FinanceNews

        digest_text = f"{item.title or ''} {item.content or ''}".strip() or (item.title or item.url or "")
        source_id = self._source_id(item)
        digest = hashlib.md5(digest_text.encode("utf-8")).hexdigest() if digest_text else content_hash(source_id)
        existing = (
            await session.execute(
                select(FinanceNews).where(
                    FinanceNews.source == item.source,
                    FinanceNews.source_id == source_id,
                )
            )
        ).scalar_one_or_none()

        raw_values = {
            "source": item.source,
            "title": item.title,
            "content": item.content,
            "url": item.url,
            "publish_time": _local_time(item.publish_time) or _news_now(),
            "source_id": source_id,
            "digest": digest,
            "category": item.category or "other",
            "related_codes": json.dumps(sorted(set(item.related_codes or []) | {
                e["code"] for e in json.loads(version.entity_evidence_json)
            }), ensure_ascii=False),
            "related_sectors": json.dumps(item.related_sectors or [], ensure_ascii=False),
        }
        if existing:
            for key, value in raw_values.items():
                setattr(existing, key, value)
            # A revised body cannot display the old body's NLP as current.
            latest_analysis = await session.scalar(select(NewsAnalysisVersion.id).where(
                NewsAnalysisVersion.content_version_id == version.id,
                NewsAnalysisVersion.status.in_(("analyzed", "fallback")),
            ).limit(1))
            if latest_analysis is None:
                existing.nlp_status = "raw"
                existing.sentiment = "neutral"
                existing.bull_bear = "neutral"
                existing.bull_bear_confidence = 0.0
                existing.importance = 5
                existing.summary = (item.content or item.title or "")[:160]
                existing.events_json = "[]"
                existing.nlp_analyzed_at = None
                existing.nlp_error = None
                existing.sentiment_method = None
                existing.events_method = None
                existing.impact_scope = "stock"
                existing.impact_reason = None
            return version

        session.add(FinanceNews(
            **raw_values,
            sentiment="neutral",
            importance=5,
            summary=(item.content or item.title or "")[:160],
            events_json="[]",
            impact_scope="stock",
            bull_bear="neutral",
            bull_bear_confidence=0.0,
            nlp_status="raw",
        ))

    async def _save_to_db(self, session, item: NewsItem, nlp_result: dict):
        """Append the exact result; FinanceNews remains a mutable page projection."""
        version_id = getattr(item, "_news_content_version_id", None)
        version = await session.get(NewsContentVersion, version_id) if version_id else None
        if version is None:
            version = await self._capture_content(session, item, observed=False)
        if version.content_hash != _raw_hash(item):
            raise ValueError("news analysis input revision mismatch")
        # New validation is frozen in this NEW analysis at its real available_at.
        # Never update a content version or borrow today's dictionary in replay.
        entities = (await verify_news_entities(session, item.title, item.content)
                    if version.origin == "observed" else [])
        nlp_result = guard_news_analysis(item.title, item.content, nlp_result, entities)
        version = await self._append_analysis(session, item, nlp_result)
        latest_id = await session.scalar(select(NewsContentVersion.id).where(
            NewsContentVersion.news_id == version.news_id,
        ).order_by(NewsContentVersion.id.desc()).limit(1))
        # A slow result for A must not overwrite the page after B was received.
        if latest_id != version.id:
            return
        from app.models.news import FinanceNews

        digest_text = f"{item.title or ''} {item.content or ''}".strip() or (item.title or "")
        source_id = self._source_id(item)
        values = {
            "source": item.source,
            "title": item.title,
            "content": item.content,
            "url": item.url,
            "publish_time": _local_time(item.publish_time) or _news_now(),
            "source_id": source_id,
            "digest": hashlib.md5(digest_text.encode("utf-8")).hexdigest() if digest_text else content_hash(source_id),
            "sentiment": nlp_result.get("sentiment"),
            "importance": self._calc_importance(nlp_result),
            "category": item.category or self._guess_category(nlp_result),
            "summary": nlp_result.get("summary"),
            "events_json": json.dumps(nlp_result.get("events", []), ensure_ascii=False),
            "related_codes": json.dumps(nlp_result.get("related_codes", []), ensure_ascii=False),
            "related_sectors": json.dumps(nlp_result.get("related_sectors", []), ensure_ascii=False),
            "impact_scope": nlp_result.get("impact_scope"),
            "bull_bear": "bull" if nlp_result.get("sentiment") == "bullish" else
                         "bear" if nlp_result.get("sentiment") == "bearish" else "neutral",
            "bull_bear_confidence": nlp_result.get("confidence", 0),
            "nlp_status": _analysis_status(nlp_result),
            "sentiment_method": nlp_result.get("sentiment_method"),
            "events_method": nlp_result.get("events_method"),
            "nlp_error": None,
            "nlp_analyzed_at": _local_time(getattr(item, "_news_analysis_completed_at", None)) or _news_now(),
        }
        existing = (
            await session.execute(
                select(FinanceNews).where(
                    FinanceNews.source == item.source,
                    FinanceNews.source_id == source_id,
                )
            )
        ).scalar_one_or_none()
        if existing:
            for key, value in values.items():
                setattr(existing, key, value)
            return
        session.add(FinanceNews(**values))

    def _calc_importance(self, nlp_result: dict) -> int:
        """计算新闻重要性 1-10"""
        score = 5  # 默认中等

        # 情感置信度
        confidence = nlp_result.get("confidence", 0)
        if confidence > 0.8:
            score += 2
        elif confidence > 0.5:
            score += 1

        # 影响范围
        scope = nlp_result.get("impact_scope", "stock")
        if scope == "market":
            score += 2
        elif scope == "sector":
            score += 1

        # 事件数量
        events = nlp_result.get("events", [])
        if len(events) >= 2:
            score += 1

        return min(10, max(1, score))

    def _guess_category(self, nlp_result: dict) -> str:
        """猜测新闻类别"""
        events = nlp_result.get("events", [])
        if events:
            evt_type = events[0].get("type", "")
            type_map = {
                "policy": "policy",
                "earnings": "earnings",
                "m&a": "m&a",
                "equity": "equity",
                "violation": "violation",
                "product": "industry",
                "rating": "rating",
            }
            return type_map.get(evt_type, "other")
        return "other"


# 全局引擎
news_engine = NewsEngine()
