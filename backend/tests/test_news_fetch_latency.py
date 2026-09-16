"""Bounded source I/O and honest transport-vs-content news freshness."""
import asyncio
import threading
from datetime import datetime, timedelta

import pytest

from app.config.settings import settings
from app.news.engine import NewsEngine
from app.news.sources.base import NewsItem


@pytest.mark.asyncio
async def test_slow_source_is_bounded_without_spawning_duplicate_threads(monkeypatch):
    release = threading.Event()
    calls = []
    item = NewsItem(source="slow", title="existing announcement", publish_time=datetime.now())

    class SlowSource:
        source_name = "slow"

        async def fetch_latest(self, limit=30):
            calls.append(limit)
            release.wait(timeout=2)
            return [item]

    class FastSource:
        source_name = "fast"

        async def fetch_latest(self, limit=30):
            return [NewsItem(source="fast", title="fast")]

    engine = NewsEngine()
    engine.sources = {"slow": SlowSource(), "fast": FastSource()}
    monkeypatch.setattr(settings, "NEWS_FETCH_SOURCE_TIMEOUT_SEC", 0.03)
    try:
        result = await engine.fetch_all()
        assert [row.source for row in result] == ["fast"]
        pending = engine._fetch_tasks["slow"]
        result = await engine.fetch_all()
        assert [row.source for row in result] == ["fast"]
        assert engine._fetch_tasks["slow"] is pending
        assert len(calls) == 1
        assert engine.get_fetch_health()["slow"]["status"] == "timeout"
        release.set()
        await asyncio.wait_for(asyncio.shield(pending), 1)
        actual_received_at = item._news_received_at
        result = await engine.fetch_all()
        assert next(row for row in result if row.source == "slow")._news_received_at == actual_received_at
        assert {row.source for row in result} == {"slow", "fast"}
        assert len(calls) == 1
        assert engine.get_fetch_health()["slow"]["status"] == "ok"
    finally:
        release.set()
        tasks = list(engine._fetch_tasks.values())
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        engine.cancel_pending_fetches()


@pytest.mark.asyncio
@pytest.mark.parametrize("payload,expected", [([], "no_data"), (None, "failed"), ([{}], "failed")])
async def test_empty_or_malformed_source_never_certifies_health(payload, expected):
    class Source:
        source_name = "test"

        async def fetch_latest(self, limit=30):
            return payload

    engine = NewsEngine()
    engine.sources = {"test": Source()}
    assert await engine.fetch_all() == []
    assert engine.get_fetch_health()["test"]["status"] == expected
    assert not engine._fetch_tasks


@pytest.mark.asyncio
async def test_source_exception_is_isolated():
    class Source:
        source_name = "test"

        async def fetch_latest(self, limit=30):
            raise RuntimeError("upstream unavailable")

    engine = NewsEngine()
    engine.sources = {"test": Source()}
    assert await engine.fetch_all() == []
    assert engine.get_fetch_health()["test"]["status"] == "failed"


@pytest.mark.asyncio
async def test_successful_poll_of_old_article_keeps_original_publication_time():
    old = datetime.now() - timedelta(days=2)

    class Source:
        source_name = "cninfo"

        async def fetch_latest(self, limit=30):
            return [NewsItem(source="cninfo", title="old announcement", publish_time=old)]

    engine = NewsEngine()
    engine.sources = {"cninfo": Source()}
    result = await engine.fetch_all()
    assert result[0].publish_time == old
    assert engine.get_fetch_health()["cninfo"]["status"] == "ok"
    assert engine.get_fetch_health()["cninfo"]["observed_at"] > old
