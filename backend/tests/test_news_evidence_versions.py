"""News PIT evidence regression: local in-memory databases and stub NLP only."""
import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import pytest_asyncio
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.models.news import FinanceNews, NewsContentVersion, NewsAnalysisVersion
from app.models.stock import StockSpot
from app.news.engine import NewsEngine
from app.news.sources.base import NewsItem
from app.news.catalyst import (
    load_news_evidence_as_of, load_direct_stock_catalyst_map, verify_news_entities,
)

T0 = datetime(2026, 8, 17, 10)
GOOD = {
    "sentiment": "bullish", "confidence": 0.9, "impact_scope": "stock",
    "events": [], "sentiment_method": "ai", "events_method": "ai",
    "related_codes": ["600002"], "related_sectors": ["半导体"], "summary": "模型摘要",
}


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        for model in (FinanceNews, StockSpot, NewsContentVersion, NewsAnalysisVersion):
            await conn.run_sync(lambda sync, m=model: m.__table__.create(sync))
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        session.add_all([StockSpot(code="600001", name="测试股份"),
                         StockSpot(code="600002", name="另一股份")])
        await session.commit()
        yield session
    await engine.dispose()


@pytest.fixture
def clock(monkeypatch):
    value = [T0]
    monkeypatch.setattr("app.news.engine._news_now", lambda: value[0])
    return value


def item(title="测试股份重大资产重组", content="测试股份签订合同", source_id="one", **kw):
    return NewsItem(source="cninfo", source_id=source_id, title=title, content=content,
                    publish_time=kw.pop("publish_time", T0 - timedelta(hours=1)), **kw)


async def evidence(db, at=T0):
    return await load_news_evidence_as_of(db, as_of_at=at)


@pytest.mark.asyncio
async def test_raw_receipt_refetch_never_refreshes_original_clocks(db, clock):
    engine = NewsEngine()
    raw = item()
    await engine.cache_raw_items(db, [raw])
    first = (await evidence(db))[0]
    page = await db.scalar(sa.select(FinanceNews))
    crawl = page.crawl_time
    clock[0] += timedelta(hours=4)
    await engine.cache_raw_items(db, [raw])
    again = (await evidence(db, clock[0]))[0]
    assert again.content_version_id == first.content_version_id
    assert again.first_received_at == again.content_available_at == T0
    assert page.crawl_time == crawl
    assert again.related_codes == ["600001"]  # actual name, not AI's code
    assert await evidence(db, T0 - timedelta(microseconds=1)) == []
    assert await db.scalar(sa.select(sa.func.count()).select_from(NewsContentVersion)) == 1


@pytest.mark.asyncio
async def test_analysis_late_completion_and_available_clock_no_backfill(db, clock):
    engine = NewsEngine()
    raw = item()
    await engine.cache_raw_items(db, [raw])
    clock[0] = T0 + timedelta(hours=2)
    await engine._save_to_db(db, raw, GOOD)
    await db.commit()
    before = (await evidence(db, T0 + timedelta(hours=1)))[0]
    after = (await evidence(db, clock[0]))[0]
    assert before.nlp_status == "raw" and before.related_sectors == []
    assert before.analysis_version_id is None
    assert after.related_sectors == ["半导体"] and after.related_codes == ["600001"]
    assert after.analysis_completed_at == after.available_at == clock[0]
    assert after.content_available_at == T0


@pytest.mark.asyncio
async def test_body_revision_late_old_nlp_does_not_cross_revision_or_page(db, clock):
    engine = NewsEngine()
    old = item()
    await engine.cache_raw_items(db, [old])
    old_version = (await evidence(db))[0].content_version_id
    old._news_content_version_id = old_version
    clock[0] += timedelta(hours=1)
    new = item(title="测试股份终止重大资产重组", content="测试股份终止合同")
    await engine.cache_raw_items(db, [new])
    clock[0] += timedelta(hours=1)
    await engine._save_to_db(db, old, GOOD)
    await db.commit()
    current = (await evidence(db, clock[0]))[0]
    assert current.content_version_id != old_version
    assert current.title == new.title and current.nlp_status == "raw"
    assert current.related_sectors == [] and current.analysis_version_id is None
    page = await db.scalar(sa.select(FinanceNews))
    assert page.title == new.title and page.nlp_status == "raw"
    assert await load_direct_stock_catalyst_map(db, T0.date(), news_end_time=clock[0]) == {}
    assert (await evidence(db, T0))[0].title == old.title


@pytest.mark.asyncio
async def test_revision_reversion_and_identical_title_body_change_are_not_deduped(db, clock):
    engine = NewsEngine()
    a, b = item(content="测试股份第一版"), item(content="测试股份第二版")
    await engine.cache_raw_items(db, [a, a, b, a])
    versions = list((await db.execute(sa.select(NewsContentVersion).order_by(NewsContentVersion.id))).scalars())
    assert len(versions) == 3
    assert versions[0].content_hash == versions[2].content_hash != versions[1].content_hash
    assert (await evidence(db))[0].content_version_id == versions[2].id
    assert all(v.first_received_at == T0 for v in versions)


@pytest.mark.asyncio
async def test_unknown_legacy_reanalysis_cannot_become_historical_pit(db, clock):
    engine = NewsEngine()
    raw = item()
    db.add(FinanceNews(source=raw.source, source_id=raw.source_id, title=raw.title,
                       content=raw.content, publish_time=raw.publish_time,
                       crawl_time=T0 - timedelta(days=3), related_codes='["600001"]',
                       sentiment="bullish", nlp_status="analyzed", nlp_analyzed_at=T0))
    await db.commit()
    await engine._save_to_db(db, raw, GOOD)
    await db.commit()
    legacy = await db.scalar(sa.select(NewsContentVersion))
    assert legacy.origin == "legacy_unknown"
    assert legacy.first_received_at is legacy.received_at is legacy.content_available_at is None
    assert await evidence(db, T0 + timedelta(days=1)) == []
    # A genuinely re-received version can be used NOW, never at the old crawl time.
    clock[0] += timedelta(hours=5)
    await engine.cache_raw_items(db, [raw])
    fresh = (await evidence(db, clock[0]))[0]
    assert fresh.first_received_at is None  # do not fabricate the lifetime first receipt
    assert fresh.content_available_at == clock[0] and fresh.nlp_status == "raw"
    assert await evidence(db, T0) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [None, {}, {"sentiment": "bullish", "confidence": float("nan")}, "raise"])
async def test_failed_empty_or_malformed_analysis_never_contributes_future_result(db, clock, monkeypatch, payload):
    engine = NewsEngine()
    async def processor(raw):
        # Raw input must already be persisted before the external await.
        assert await db.scalar(sa.select(sa.func.count()).select_from(NewsContentVersion)) == 1
        clock[0] += timedelta(minutes=1)
        if payload == "raise":
            raise RuntimeError("no network")
        return payload
    monkeypatch.setattr("app.news.engine.news_processor.process", processor)
    assert await engine.process_and_store([item()], db_session=db) == []
    attempt = await db.scalar(sa.select(NewsAnalysisVersion))
    assert attempt.status == "failed"
    assert attempt.analysis_completed_at == clock[0]
    value = (await evidence(db, clock[0]))[0]
    assert value.nlp_status == "raw" and value.related_sectors == []


@pytest.mark.asyncio
async def test_failed_retry_does_not_reuse_old_success_but_preserves_old_replay(db, clock):
    engine = NewsEngine()
    raw = item()
    await engine._save_to_db(db, raw, GOOD)
    await db.commit()
    assert (await evidence(db))[0].nlp_status == "analyzed"
    clock[0] += timedelta(minutes=1)
    await engine._append_analysis(db, raw, {}, status="failed")
    await db.commit()
    assert (await evidence(db, clock[0]))[0].nlp_status == "raw"
    assert (await evidence(db))[0].nlp_status == "analyzed"


@pytest.mark.asyncio
async def test_entity_exact_name_code_validation_and_mapping_frozen(db, clock):
    assert await verify_news_entities(db, "测试股份600001重大合同", "") == [
        {"code": "600001", "name": "测试股份", "method": "exact_unique_stock_name_v1"}]
    for text in ["600001重大合同", "测试股份600002重大合同", "股份重大合同", "AI关联600003"]:
        assert await verify_news_entities(db, text, "") == []
    await NewsEngine().cache_raw_items(db, [item()])
    stock = await db.get(StockSpot, "600001")
    stock.name = "已经改名"
    await db.commit()
    assert (await evidence(db))[0].related_codes == ["600001"]
    db.add(StockSpot(code="600003", name="另一股份"))
    await db.commit()
    assert await verify_news_entities(db, "另一股份重大合同", "") == []


@pytest.mark.asyncio
async def test_ai_hallucinated_codes_do_not_create_direct_catalyst(db, clock):
    engine = NewsEngine()
    raw = item(title="全行业出现重大资产重组", content="", related_codes=["600001"])
    await engine._save_to_db(db, raw, GOOD)
    await db.commit()
    assert (await evidence(db))[0].related_codes == []
    assert await load_direct_stock_catalyst_map(db, T0.date(), news_end_time=T0) == {}


@pytest.mark.asyncio
async def test_overnight_phase_uses_availability_not_old_publish_clock(db, clock):
    clock[0] = T0.replace(hour=18)
    await NewsEngine().cache_raw_items(db, [item()])
    result = await load_direct_stock_catalyst_map(db, T0.date(), news_end_time=clock[0])
    context = result["600001"]
    assert context["news_fresh_after_trade_close"] is True
    assert context["news_after_close_count"] == 1 and context["news_before_close_count"] == 0
    assert context["news_publish_time"] == (T0 - timedelta(hours=1)).isoformat()
    assert context["news_evidence_protocol"] == "news_pit_v1"


@pytest.mark.asyncio
async def test_empty_missing_publish_and_timezone_cutoff(db, clock):
    assert await NewsEngine().cache_raw_items(db, []) == 0
    assert await evidence(db) == []
    await NewsEngine().cache_raw_items(db, [item(publish_time=None)])
    assert await evidence(db) == []  # projection's synthetic publish time is not evidence
    await NewsEngine().cache_raw_items(db, [item(source_id="dated")])
    aware = T0.replace(tzinfo=timezone(timedelta(hours=8))).astimezone(timezone.utc)
    assert len(await evidence(db, aware)) == 1


@pytest.mark.asyncio
async def test_orm_and_bulk_update_delete_are_rejected(db, clock):
    await NewsEngine().cache_raw_items(db, [item()])
    version = await db.scalar(sa.select(NewsContentVersion))
    version.origin = "changed"
    with pytest.raises(ValueError, match="append-only"):
        await db.flush()
    await db.rollback()
    for statement in ("UPDATE news_content_version SET origin='changed'", "DELETE FROM news_content_version"):
        with pytest.raises(sa.exc.IntegrityError, match="append-only"):
            await db.execute(sa.text(statement))
        await db.rollback()


def test_migration_idempotent_roundtrip_keeps_legacy_bytes_and_adds_no_pit(tmp_path):
    file = Path(__file__).parents[1] / "alembic/versions/029_news_evidence_versions.py"
    spec = importlib.util.spec_from_file_location("news_evidence_migration", file)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    assert migration.down_revision == "028_factor_evaluation_runs"
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'migration.db'}")
    with engine.begin() as conn:
        FinanceNews.__table__.create(conn)
        conn.execute(sa.text("INSERT INTO finance_news (source, source_id, title, publish_time, crawl_time) VALUES ('cls','old','旧历史','2020-01-01','2020-01-02')"))
        old = conn.execute(sa.text("SELECT * FROM finance_news")).all()
        with Operations.context(MigrationContext.configure(conn)):
            migration.upgrade()
            migration.upgrade()
            assert conn.scalar(sa.text("SELECT COUNT(*) FROM news_content_version")) == 0
            assert conn.scalar(sa.text("SELECT COUNT(*) FROM news_analysis_version")) == 0
            triggers = conn.execute(sa.text("SELECT name FROM sqlite_master WHERE type='trigger'")).all()
            assert len(triggers) == 4
            assert conn.execute(sa.text("SELECT * FROM finance_news")).all() == old
            migration.downgrade()
            migration.downgrade()
            assert conn.execute(sa.text("SELECT * FROM finance_news")).all() == old
            assert not sa.inspect(conn).has_table("news_content_version")
            migration.upgrade()
            assert conn.scalar(sa.text("SELECT COUNT(*) FROM news_content_version")) == 0
    engine.dispose()


@pytest.mark.asyncio
async def test_identical_analysis_refetch_does_not_refresh_availability(db, clock):
    engine = NewsEngine()
    raw = item()
    await engine._save_to_db(db, raw, GOOD)
    await db.commit()
    original = (await evidence(db))[0]
    clock[0] = T0.replace(hour=18)
    await engine._save_to_db(db, raw, dict(GOOD))
    await db.commit()
    repeated = (await evidence(db, clock[0]))[0]
    assert repeated.analysis_version_id == original.analysis_version_id
    assert repeated.available_at == original.available_at == T0
    result = await load_direct_stock_catalyst_map(db, T0.date(), news_end_time=clock[0])
    assert result["600001"]["news_fresh_after_trade_close"] is False
    assert await db.scalar(sa.select(sa.func.count()).select_from(NewsAnalysisVersion)) == 1


@pytest.mark.asyncio
async def test_completion_clock_is_not_persistence_clock(db, clock):
    engine = NewsEngine()
    raw = item()
    await engine.cache_raw_items(db, [raw])
    raw._news_analysis_completed_at = T0 + timedelta(minutes=5)
    clock[0] = T0 + timedelta(minutes=10)
    await engine._save_to_db(db, raw, GOOD)
    await db.commit()
    assert (await evidence(db, T0 + timedelta(minutes=7)))[0].nlp_status == "raw"
    current = (await evidence(db, clock[0]))[0]
    assert current.analysis_completed_at == T0 + timedelta(minutes=5)
    assert current.available_at == clock[0]


@pytest.mark.asyncio
async def test_reanalysis_negative_result_and_fallback_use_only_current_asof(db, clock):
    engine = NewsEngine()
    raw = item()
    await engine._save_to_db(db, raw, GOOD)
    await db.commit()
    clock[0] += timedelta(minutes=30)
    negative = {**GOOD, "sentiment": "bearish", "sentiment_method": "keyword", "events_method": "keyword"}
    await engine._save_to_db(db, raw, negative)
    await db.commit()
    assert (await evidence(db, clock[0]))[0].nlp_status == "fallback"
    assert await load_direct_stock_catalyst_map(db, T0.date(), news_end_time=clock[0]) == {}
    assert "600001" in await load_direct_stock_catalyst_map(db, T0.date(), news_end_time=T0)
    assert await db.scalar(sa.select(sa.func.count()).select_from(NewsAnalysisVersion)) == 2


@pytest.mark.asyncio
async def test_future_revision_publish_time_does_not_resurrect_older_version(db, clock):
    engine = NewsEngine()
    await engine.cache_raw_items(db, [item()])
    clock[0] += timedelta(hours=1)
    await engine.cache_raw_items(db, [item(publish_time=T0 + timedelta(days=1))])
    assert await evidence(db, clock[0]) == []
    assert len(await evidence(db, T0)) == 1


@pytest.mark.asyncio
async def test_pit_reads_do_not_query_mutable_page_or_stock_dictionary(db, clock):
    await NewsEngine().cache_raw_items(db, [item()])
    statements = []
    sync = db.bind.sync_engine
    def capture(conn, cursor, statement, params, context, executemany):
        statements.append(statement.lower())
    sa.event.listen(sync, "before_cursor_execute", capture)
    try:
        result = await evidence(db)
        assert result[0].related_codes == ["600001"]
        assert all("finance_news" not in stmt and "stock_spot" not in stmt for stmt in statements)
        assert all(stmt.lstrip().startswith("select") for stmt in statements)
    finally:
        sa.event.remove(sync, "before_cursor_execute", capture)


@pytest.mark.asyncio
async def test_missing_dictionary_fails_closed_without_partial_raw_row(clock):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        for model in (FinanceNews, NewsContentVersion, NewsAnalysisVersion):
            await conn.run_sync(lambda sync, m=model: m.__table__.create(sync))
    async with async_sessionmaker(engine, expire_on_commit=False)() as db:
        assert await NewsEngine().cache_raw_items(db, [item()]) == 0
        assert await db.scalar(sa.select(sa.func.count()).select_from(FinanceNews)) == 0
        assert await db.scalar(sa.select(sa.func.count()).select_from(NewsContentVersion)) == 0
    await engine.dispose()


@pytest.mark.asyncio
async def test_same_object_reused_in_revision_batch_binds_each_exact_input(db, clock, monkeypatch):
    engine = NewsEngine()
    a, b = item(content="测试股份第一版"), item(content="测试股份第二版")
    async def processor(raw):
        return {**GOOD, "summary": raw.content}
    monkeypatch.setattr("app.news.engine.news_processor.process", processor)
    # Treat this batch as actual adapter receipts rather than reconstructed page rows.
    a._news_received_at = b._news_received_at = T0
    assert len(await engine.process_and_store([a, b, a], db_session=db)) == 3
    attempts = list((await db.execute(sa.select(NewsAnalysisVersion).order_by(NewsAnalysisVersion.id))).scalars())
    assert len({v.content_version_id for v in attempts}) == 3
    assert [json.loads(v.result_json)["summary"] for v in attempts] == [a.content, b.content, a.content]


@pytest.mark.asyncio
async def test_multiple_analysis_versions_count_once_per_article(db, clock):
    engine = NewsEngine()
    raw = item()
    await engine._save_to_db(db, raw, GOOD)
    await db.commit()
    clock[0] += timedelta(minutes=1)
    await engine._save_to_db(db, raw, {**GOOD, "confidence": 0.8})
    await db.commit()
    result = await load_direct_stock_catalyst_map(db, T0.date(), news_end_time=clock[0])
    assert result["600001"]["news_count"] == 1
    assert result["600001"]["news_repeated_direct"] is False
    assert len(result["600001"]["news_evidence"]["versions"]) == 1
    assert result["600001"]["news_evidence"]["versions"][0]["content_version_id"] == result["600001"]["news_content_version_id"]


@pytest.mark.asyncio
async def test_latest_unknown_protocol_or_corrupt_analysis_cannot_reuse_older_success(db, clock):
    import hashlib
    engine = NewsEngine()
    raw = item()
    await engine._save_to_db(db, raw, GOOD)
    await db.commit()
    v = (await evidence(db))[0]
    for index, protocol in enumerate(("future_protocol", "news_pit_v1"), 1):
        clock[0] = T0 + timedelta(minutes=index)
        result_json = json.dumps({"sentiment": "bullish", "confidence": True, "importance": 10})
        db.add(NewsAnalysisVersion(
            content_version_id=v.content_version_id, status="analyzed",
            analysis_completed_at=clock[0], available_at=clock[0],
            result_json=result_json, result_hash=hashlib.sha256(result_json.encode()).hexdigest(),
            protocol_version=protocol,
        ))
        await db.commit()
        assert (await evidence(db, clock[0]))[0].nlp_status == "raw"
    assert (await evidence(db, T0))[0].nlp_status == "analyzed"
    for statement in ("UPDATE news_analysis_version SET status='failed'", "DELETE FROM news_analysis_version"):
        with pytest.raises(sa.exc.IntegrityError, match="append-only"):
            await db.execute(sa.text(statement))
        await db.rollback()


@pytest.mark.asyncio
async def test_unknown_new_content_invalidate_older_known_content_without_becoming_pit(db, clock):
    engine = NewsEngine()
    await engine.cache_raw_items(db, [item()])
    clock[0] += timedelta(minutes=1)
    # Simulates a revised legacy page row with no captured transport receipt.
    changed = item(title="测试股份终止重组", content="旧页面人工修订，接收时间未知")
    await engine._save_to_db(db, changed, GOOD)
    await db.commit()
    assert await evidence(db, clock[0]) == []
    assert (await evidence(db, T0))[0].title == item().title

