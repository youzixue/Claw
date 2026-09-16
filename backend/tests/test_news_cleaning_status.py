"""清洗来源展示与既有分析口径兼容。"""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.v1.news import _news_analysis_status, _news_payload
from app.models.news import FinanceNews


@pytest.mark.asyncio
async def test_status_breakdown_preserves_processed_count():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(lambda sync: FinanceNews.__table__.create(sync))
    session = async_sessionmaker(engine, expire_on_commit=False)
    async with session() as db:
        now = datetime.now()
        for index, status in enumerate(["analyzed", "fallback", "failed", "raw", "analyzing", "analyzing"]):
            db.add(FinanceNews(
                source="cls", source_id=str(index), title=f"新闻 {index}",
                publish_time=now, nlp_status=status,
                nlp_analyzed_at=now - timedelta(hours=2) if index == 5 else now,
            ))
        await db.commit()
        result = await _news_analysis_status(db)
        assert result["fetched_count"] == 6
        assert result["analyzed_count"] == 2
        assert result["ai_analyzed_count"] == 1
        assert result["fallback_count"] == 1
        assert result["failed_count"] == 1
        assert result["analyzing_count"] == 1
        assert result["pending_count"] == 3  # failed + raw + stale analyzing
        assert (await _news_analysis_status(db, source="missing"))["fetched_count"] == 0
    await engine.dispose()


@pytest.mark.parametrize("status,label", [
    ("fallback", "规则降级"), ("failed", "失败待重试"), ("analyzing", "清洗中"),
])
def test_payload_labels_do_not_misrepresent_rules_as_ai(status, label):
    news = SimpleNamespace(
        id=1, source="cls", title="测试", content="", url="", publish_time=datetime.now(),
        category="", related_codes="[]", related_sectors="[]", bull_bear_confidence=0.5,
        sentiment="neutral", impact_scope="stock", importance=5, events_json="[]",
        summary="", bull_bear="neutral", nlp_status=status,
    )
    payload = _news_payload(news)
    assert payload["nlp_status_label"] == label
    assert payload["is_analyzed"] is (status == "fallback")


@pytest.mark.asyncio
async def test_one_job_does_not_retry_same_failed_news_and_starve_older_rows(monkeypatch):
    from app.api.v1 import news
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(lambda sync: FinanceNews.__table__.create(sync))
    session = async_sessionmaker(engine, expire_on_commit=False)
    async def fail_processor(*_, **__):
        return []
    monkeypatch.setattr(news.news_engine, "process_and_store", fail_processor)
    async with session() as db:
        now = datetime.now()
        db.add_all([
            FinanceNews(source="cls", source_id="new", title="最新失败", publish_time=now, nlp_status="raw"),
            FinanceNews(source="cls", source_id="old", title="较早新闻", publish_time=now - timedelta(hours=1), nlp_status="raw"),
        ])
        await db.commit()
        first = await news._analyze_pending_window(db, limit=1)
        assert first["failed"] == 1
        second = await news._analyze_pending_window(db, limit=1, exclude_ids=set(first["attempted_ids"]))
        assert second["failed"] == 1
        assert set(first["attempted_ids"]).isdisjoint(second["attempted_ids"])
    await engine.dispose()
