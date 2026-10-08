"""Real local WAL contention: news batches must release their own transactions."""
import asyncio

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.news import FinanceNews, NewsAnalysisVersion, NewsContentVersion
from app.models.stock import StockSpot
from app.news.engine import NewsEngine
from test_news_evidence_versions import GOOD, item


@pytest_asyncio.fixture
async def sessions(tmp_path):
    engine = create_async_engine(
        "sqlite+aiosqlite:///" + str(tmp_path / "news.db"),
        connect_args={"timeout": 0.1},
    )
    async with engine.begin() as conn:
        await conn.execute(text("PRAGMA journal_mode=WAL"))
        for model in (FinanceNews, StockSpot, NewsContentVersion, NewsAnalysisVersion):
            await conn.run_sync(model.__table__.create)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as db:
        db.add(StockSpot(code="600001", name="测试股份"))
        await db.commit()
    try:
        yield maker
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["raw", "before_nlp"])
async def test_news_releases_writer_between_items(sessions, monkeypatch, mode):
    engine = NewsEngine()
    original = engine._save_raw_to_db
    reached_second = asyncio.Event()
    continue_second = asyncio.Event()

    async def instrument(db, raw, **kwargs):
        if raw.source_id == "second":
            reached_second.set()
            await continue_second.wait()
        return await original(db, raw, **kwargs)

    async def nlp(raw):
        return dict(GOOD)

    monkeypatch.setattr(engine, "_save_raw_to_db", instrument)
    monkeypatch.setattr("app.news.engine.news_processor.process", nlp)
    raws = [item(source_id="first"), item(source_id="second")]
    async with sessions() as scanner:
        work = (engine.cache_raw_items(scanner, raws) if mode == "raw" else
                engine.process_and_store(raws, db_session=scanner))
        task = asyncio.create_task(work)
        try:
            await asyncio.wait_for(reached_second.wait(), 3)
            async with sessions() as writer:
                await writer.execute(text("UPDATE stock_spot SET name='另一股份'"))
                await writer.commit()
                assert await writer.scalar(select(func.count()).select_from(NewsContentVersion)) == 1
            continue_second.set()
            result = await asyncio.wait_for(task, 3)
            assert (result if mode == "raw" else len(result)) == 2
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    async with sessions() as db:
        assert await db.scalar(select(func.count()).select_from(NewsContentVersion)) == 2


@pytest.mark.asyncio
async def test_raw_busy_snapshot_does_not_poison_later_items(sessions, monkeypatch):
    engine = NewsEngine()
    original = engine._save_raw_to_db
    observed_errors = []

    async def instrument(db, raw, **kwargs):
        if raw.source_id == "first":
            # SAVEPOINT + SELECT pins the actual WAL snapshot, not just the
            # SQLAlchemy virtual transaction. A competing writer advances WAL.
            await db.execute(select(StockSpot.code))
            async with sessions() as writer:
                await writer.execute(text("UPDATE stock_spot SET name='另一股份'"))
                await writer.commit()
        try:
            return await original(db, raw, **kwargs)
        except Exception as exc:
            observed_errors.append(getattr(getattr(exc, "orig", None), "sqlite_errorcode", None))
            raise

    monkeypatch.setattr(engine, "_save_raw_to_db", instrument)
    async with sessions() as db:
        await db.execute(text("BEGIN"))
        saved = await engine.cache_raw_items(
            db, [item(source_id="first"), item(source_id="second")],
        )
        assert observed_errors == [517]  # SQLITE_BUSY_SNAPSHOT, not synthetic "locked"
        assert saved == 1
    async with sessions() as db:
        assert list(await db.scalars(select(FinanceNews.source_id))) == ["second"]
        assert await db.scalar(select(func.count()).select_from(NewsContentVersion)) == 1
        assert await db.scalar(select(func.count()).select_from(NewsAnalysisVersion)) == 0


@pytest.mark.asyncio
async def test_failed_raw_item_does_not_erase_prior_committed_evidence(sessions, monkeypatch):
    engine = NewsEngine()
    original = engine._save_raw_to_db

    async def instrument(db, raw, **kwargs):
        result = await original(db, raw, **kwargs)
        if raw.source_id == "bad":
            raise ValueError("invalid article after flush")
        return result

    monkeypatch.setattr(engine, "_save_raw_to_db", instrument)
    async with sessions() as db:
        saved = await engine.cache_raw_items(
            db, [item(source_id="first"), item(source_id="bad"), item(source_id="last")],
        )
        assert saved == 2
    async with sessions() as db:
        assert set(await db.scalars(select(FinanceNews.source_id))) == {"first", "last"}
        assert await db.scalar(select(func.count()).select_from(NewsContentVersion)) == 2


@pytest.mark.asyncio
async def test_failed_commit_cannot_publish_article_via_top_level_savepoint(sessions, monkeypatch):
    async with sessions() as db:
        async def failed_commit():
            raise RuntimeError("commit not performed")
        monkeypatch.setattr(db, "commit", failed_commit)
        assert await NewsEngine().cache_raw_items(db, [item(source_id="not-committed")]) == 0
    async with sessions() as db:
        assert await db.scalar(select(func.count()).select_from(FinanceNews)) == 0
        assert await db.scalar(select(func.count()).select_from(NewsContentVersion)) == 0
