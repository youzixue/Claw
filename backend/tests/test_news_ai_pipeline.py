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
