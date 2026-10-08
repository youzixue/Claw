"""News NLP uses real dispatch/normalization, with model replies isolated from production."""
from unittest.mock import AsyncMock

import pytest

from app.ai.provider import ai_provider
from app.ai.event_extractor import extract_events
from app.news.engine import _analysis_status
from app.news.nlp.processor import news_processor
from app.news.sources.base import NewsItem


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", [None, [], ["bad"], "text", 5, True])
async def test_invalid_model_json_falls_back_without_crashing(monkeypatch, reply):
    monkeypatch.setattr(ai_provider, "chat_json", AsyncMock(return_value=reply))
    result = await news_processor.process(NewsItem(source="test", title="测试公司发布业绩预增公告"))
    assert result["sentiment_method"] == "keyword"
    assert result["events_method"] == "keyword"
    assert _analysis_status(result) == "fallback"


@pytest.mark.asyncio
async def test_empty_ai_event_list_remains_ai_and_does_not_invent_keyword_events(monkeypatch):
    monkeypatch.setattr(ai_provider, "chat_json", AsyncMock(return_value={"events": []}))
    result = await extract_events("历史回顾：某公司曾发布业绩预增")
    assert result == {"events": [], "method": "ai"}


@pytest.mark.asyncio
@pytest.mark.parametrize("events", [None, "bad", {}, [1], ["text"]])
async def test_malformed_event_arrays_use_rules(monkeypatch, events):
    monkeypatch.setattr(ai_provider, "chat_json", AsyncMock(return_value={"events": events}))
    result = await extract_events("测试公司发布业绩预增公告")
    assert result["method"] == "keyword"


@pytest.mark.parametrize("sentiment,events,expected", [
    ("ai", "ai", "analyzed"), ("ai", "keyword", "analyzed"),
    ("keyword", "ai", "analyzed"), ("keyword", "keyword", "fallback"),
    ("fallback", "fallback", "fallback"), (None, None, "fallback"),
])
def test_cleaning_status_requires_actual_ai_participation(sentiment, events, expected):
    assert _analysis_status({"sentiment_method": sentiment, "events_method": events}) == expected


@pytest.mark.asyncio
async def test_pending_batch_uses_stored_originals_and_keeps_truncation_denominator(monkeypatch):
    from datetime import datetime
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from app.models.news import FinanceNews
    from app.news.engine import NewsEngine
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    subject = NewsEngine()
    try:
        async with engine.begin() as conn:
            await conn.run_sync(FinanceNews.__table__.create)
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            db.add_all([FinanceNews(source="cls", source_id=str(i), title=f"原文{i}", content="冻结文字",
                                   publish_time=datetime(2026,10,7,19), nlp_status="raw")
                        for i in range(3)])
            db.add(FinanceNews(source="global", source_id="SPX", title="指数不是新闻",
                               publish_time=datetime(2026,10,7,19), nlp_status="raw"))
            await db.commit()
            async def analyze(items, **kwargs):
                assert len(items) == 2 and all(item.content == "冻结文字" for item in items)
                assert all(not hasattr(item,"_news_received_at") for item in items)
                return [{"sentiment_method":"ai", "events_method":"ai"} for _ in items]
            monkeypatch.setattr(subject,"process_and_store",analyze)
            result=await subject.analyze_pending(db,limit=2)
            assert result["pending_at_start"] == 3 and result["selected"] == result["processed"] == 2
            assert result["selection_truncated"] is True and result["status"] == "partial"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_partial_ai_and_null_method_enter_cooldown_recovery_not_permanent_success(monkeypatch):
    from datetime import datetime, timedelta
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from app.models.news import FinanceNews
    from app.news.engine import NewsEngine, _news_now
    monkeypatch.setattr(ai_provider,"enabled",True)
    monkeypatch.setattr(ai_provider,"runtime_status",lambda:{"circuit_open":False})
    engine=create_async_engine("sqlite+aiosqlite:///:memory:")
    subject=NewsEngine()
    try:
        async with engine.begin() as conn:
            await conn.run_sync(FinanceNews.__table__.create)
        async with async_sessionmaker(engine,expire_on_commit=False)() as db:
            for identity,method,stamp in (("mixed","keyword",_news_now()-timedelta(hours=2)),
                                          ("null",None,_news_now()-timedelta(hours=2)),
                                          ("recent","keyword",_news_now())):
                db.add(FinanceNews(source="cls",source_id=identity,title=identity,
                       publish_time=datetime(2026,10,7,19),nlp_status="analyzed",
                       sentiment_method="ai",events_method=method,nlp_analyzed_at=stamp))
            await db.commit()
            seen=[]
            async def process(items,**kwargs):
                seen.extend(item.source_id for item in items)
                return [{"sentiment_method":"ai","events_method":"ai"} for item in items]
            monkeypatch.setattr(subject,"process_and_store",process)
            result=await subject.analyze_pending(db,limit=10)
            assert set(seen)=={"mixed","null"} and result["selected"]==2
            assert "recent" not in seen
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_failed_newest_prefix_does_not_starve_older_raw_news(monkeypatch):
    from datetime import datetime, timedelta
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from app.models.news import FinanceNews
    from app.news.engine import NewsEngine
    engine=create_async_engine("sqlite+aiosqlite:///:memory:")
    subject=NewsEngine()
    try:
        async with engine.begin() as conn:
            await conn.run_sync(FinanceNews.__table__.create)
        async with async_sessionmaker(engine,expire_on_commit=False)() as db:
            db.add_all([FinanceNews(source="cls",source_id=str(i),title=f"原文{i}",
                        publish_time=datetime(2026,10,7,19)+timedelta(minutes=i),
                        nlp_status="raw") for i in range(4)])
            await db.commit()
            attempts=[]
            async def fail(items,**kwargs):
                attempts.append({item.source_id for item in items})
                return []
            monkeypatch.setattr(subject,"process_and_store",fail)
            first=await subject.analyze_pending(db,limit=2)
            second=await subject.analyze_pending(db,limit=2)
            assert first["failed"]==second["failed"]==2
            assert attempts[0].isdisjoint(attempts[1])
            assert (await subject.analyze_pending(db,limit=2))["selected"]==0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_pending_waves_preserve_committed_progress_when_next_wave_cancelled(monkeypatch):
    import asyncio
    from datetime import datetime
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from app.models.news import FinanceNews
    from app.news.engine import NewsEngine
    from app.config.settings import settings
    monkeypatch.setattr(settings,"NEWS_AI_WAVE_SIZE",2)
    engine=create_async_engine("sqlite+aiosqlite:///:memory:")
    subject=NewsEngine()
    try:
        async with engine.begin() as conn:
            await conn.run_sync(FinanceNews.__table__.create)
        async with async_sessionmaker(engine,expire_on_commit=False)() as db:
            db.add_all([FinanceNews(source="cls",source_id=str(i),title=f"原文{i}",
                        publish_time=datetime(2026,10,7,19),nlp_status="raw") for i in range(4)])
            await db.commit()
            calls=[]
            async def wave(items,db_session,**kwargs):
                calls.append([item.source_id for item in items])
                if len(calls)>1:
                    raise asyncio.CancelledError()
                for item in items:
                    row=await db_session.scalar(select(FinanceNews).where(FinanceNews.source_id==item.source_id))
                    row.nlp_status="analyzed"
                await db_session.commit()
                return [{"sentiment_method":"ai","events_method":"ai"} for item in items]
            monkeypatch.setattr(subject,"process_and_store",wave)
            with pytest.raises(asyncio.CancelledError):
                await subject.analyze_pending(db,limit=4)
            rows=list((await db.scalars(select(FinanceNews))).all())
            assert [row.nlp_status for row in rows].count("analyzed")==2
            assert [row.nlp_status for row in rows].count("raw")==2
            assert set(calls[0]).isdisjoint(calls[1])
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_news_pipeline_preserves_structured_ai_output(monkeypatch):
    async def reply(prompt, system=""):
        if "情感分析引擎" in system:
            return {"sentiment": "bullish", "confidence": 0.8, "impact_scope": "sector",
                    "summary": "测试行业盈利改善，实际影响仍需核验。",
                    "related_sectors": ["测试行业"], "key_points": ["盈利预期改善"]}
        return {"events": [{"type": "earnings", "title": "测试业绩增长",
                             "related_codes": [], "related_sectors": ["测试行业"]}]}
    monkeypatch.setattr(ai_provider, "chat_json", reply)
    result = await news_processor.process(NewsItem(source="test", title="测试行业业绩增长"))
    assert result["sentiment_method"] == result["events_method"] == "ai"
    assert result["summary"].startswith("测试行业盈利")
    assert result["sentiment"] == "bullish"
    assert result["related_codes"] == []
    assert result["related_sectors"] == ["测试行业"]
    assert len(result["events"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("method,expected", [("ai", "analyzed"), ("keyword", "fallback")])
async def test_append_only_evidence_and_page_agree_on_cleaning_status(method, expected):
    from datetime import datetime, timedelta
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from app.models.news import FinanceNews, NewsContentVersion, NewsAnalysisVersion
    from app.news.engine import news_engine, _raw_hash, _news_now

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as connection:
            for model in (FinanceNews, NewsContentVersion, NewsAnalysisVersion):
                await connection.run_sync(model.__table__.create)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as db:
            item = NewsItem(source="test", source_id="isolated", title="隔离测试新闻", publish_time=datetime(2026, 9, 14, 15))
            row = FinanceNews(source=item.source, source_id=item.source_id, title=item.title, publish_time=item.publish_time)
            db.add(row)
            await db.flush()
            now = _news_now() - timedelta(seconds=1)
            version = NewsContentVersion(news_id=row.id, content_hash=_raw_hash(item), source=item.source,
                                         recorded_at=now, origin="legacy_unknown", payload_json="{}",
                                         entity_evidence_json="{}", entity_verified_at=now, protocol_version="test")
            db.add(version)
            await db.flush()
            item._news_content_version_id = version.id
            result = {"sentiment": "neutral", "confidence": 0.5, "impact_scope": "sector",
                      "sentiment_method": method, "events_method": "keyword", "summary": "测试摘要",
                      "events": [], "related_codes": [], "related_sectors": []}
            await news_engine._save_to_db(db, item, result)
            await db.commit()
            attempt = await db.scalar(select(NewsAnalysisVersion))
            assert attempt.status == row.nlp_status == expected
            assert row.sentiment_method == method
            assert row.summary == "测试摘要"
    finally:
        await engine.dispose()
