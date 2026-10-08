"""Local, no-main/no-network regression for the DSH premarket read contract."""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from datetime import date, datetime, time, timedelta
from types import SimpleNamespace

import pandas as pd
import pytest
import pytest_asyncio
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.dashboard2.external_sources import ExternalFactorCollector
from app.db.session import Base
from app.models.governance import DashboardSnapshot, TradeCalendarModel
from app.models.news import FinanceNews, NewsAnalysisVersion, NewsContentVersion
from app.models.review import DailyReviewSnapshot
from app.models.stock import StockKline
from app.news.catalyst import NEWS_EVIDENCE_PROTOCOL
from app.review import overnight_evidence as evidence
from app.review.service import _market_dimensions

DAY = date(2026, 9, 4)
PREVIOUS = date(2026, 9, 3)
CUTOFF = datetime(2026, 9, 4, 8, 55)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False)


@pytest_asyncio.fixture
async def local_db(tmp_path):
    temporary = TemporaryDirectory(prefix="dsh-overnight-", dir=tmp_path)
    path = Path(temporary.name) / "overnight.db"
    writer = create_async_engine(f"sqlite+aiosqlite:///{path}")
    maker = async_sessionmaker(writer, expire_on_commit=False, autoflush=False)
    reader = create_async_engine(f"sqlite+aiosqlite:///file:{path}?mode=ro&uri=true")
    statements = []

    @event.listens_for(reader.sync_engine, "connect")
    def read_only_connection(connection, _record):
        cursor = connection.cursor()
        cursor.execute("PRAGMA query_only=ON")
        cursor.close()

    @event.listens_for(reader.sync_engine, "before_cursor_execute")
    def reject_non_select(_conn, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)
        assert statement.lstrip().upper().startswith("SELECT"), statement

    read_maker = async_sessionmaker(reader, class_=AsyncSession, expire_on_commit=False)
    try:
        async with writer.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        yield SimpleNamespace(writer=writer, maker=maker, reader=reader,
                              read_maker=read_maker, statements=statements)
    finally:
        try:
            await reader.dispose()
        finally:
            try:
                await writer.dispose()
            finally:
                temporary.cleanup()


async def _calendar(db, previous=PREVIOUS, target=DAY, *, closed=()):
    day = previous
    while day <= target:
        db.add(TradeCalendarModel(trade_date=day,
                                  is_trade_day=day.weekday() < 5 and day not in closed,
                                  session_type="full"))
        day += timedelta(days=1)


async def _baseline(db, previous=PREVIOUS, target=DAY, *, closed=()):
    await _calendar(db, previous, target, closed=closed)
    db.add(StockKline(code="600001", trade_date=previous, source="ths",
                      open=10, high=11, low=9, close=10.5, prev_close=10,
                      change_pct=5, turnover=2, volume=1000, amount=10000))
    stamp = datetime.combine(previous, time(20, 30))
    row = DailyReviewSnapshot(review_key=f"post-{target}", review_date=previous,
                              analysis_trade_date=previous, phase="postmarket",
                              as_of_at=stamp, created_at=stamp, data_version="fixture-data-v1",
                              schema_version="fixture-schema-v1", quality_status="good",
                              payload_json='{"frozen":true}')
    db.add(row)
    await db.flush()
    return row


async def _content(db, *, publish=None, received=None, available=None, recorded=None,
                   origin="observed", news=None, title="测试股份签订合同", content="测试股份披露合同",
                   source_id=None, invalid_hash=False, source="test"):
    publish = publish or datetime(2026, 9, 3, 16)
    received = received or publish + timedelta(minutes=5)
    available = available or received
    recorded = recorded or received
    if news is None:
        news = FinanceNews(source=source, source_id=source_id or f"{title}-{publish}-{received}",
                           title="当前页面可能已被修订，不可倒灌", publish_time=publish,
                           nlp_status="analyzed", sentiment="positive", importance=10)
        db.add(news)
        await db.flush()
    raw = {"source": source, "title": title, "content": content,
           "publish_time": publish.isoformat(), "url": ""}
    encoded = _json(raw)
    row = NewsContentVersion(
        news_id=news.id, source=source, publish_time=publish,
        content_hash="0" * 64 if invalid_hash else hashlib.sha256(encoded.encode()).hexdigest(),
        first_received_at=received if origin == "observed" else None,
        received_at=received if origin == "observed" else None,
        recorded_at=recorded, content_available_at=available if origin == "observed" else None,
        entity_verified_at=available, origin=origin, protocol_version=NEWS_EVIDENCE_PROTOCOL,
        payload_json=encoded, entity_evidence_json=_json([
            {"code": "600001", "name": "测试股份", "method": "exact_unique_stock_name_v1"},
        ]),
    )
    db.add(row)
    await db.flush()
    return news, row


async def _analysis(db, version, *, completed=None, available=None, status="analyzed", importance=8):
    completed = completed or datetime(2026, 9, 3, 18)
    available = available or completed
    raw = {} if status == "failed" else {
        "importance": importance, "sentiment": "bullish", "confidence": 0.9,
        "summary": "模型观点，不是确定受益", "impact_scope": "stock",
        "related_sectors": ["半导体"],
    }
    encoded = _json(raw)
    row = NewsAnalysisVersion(content_version_id=version.id, status=status,
        analysis_completed_at=completed, available_at=available,
        result_json=encoded, result_hash=hashlib.sha256(encoded.encode()).hexdigest(),
        protocol_version=NEWS_EVIDENCE_PROTOCOL)
    db.add(row)
    await db.flush()
    return row


async def _read(env, *, target=DAY, cutoff=CUTOFF, limit=100):
    async with env.read_maker() as db:
        return await evidence.read_premarket_context(
            db, trade_date=target, as_of=cutoff, limit=limit,
        )


@pytest.mark.asyncio
async def test_news_pages_exhaust_holiday_window_and_bind_cutoff(local_db):
    env = local_db
    async with env.maker() as db:
        await _baseline(db)
        for i in range(57):
            await _content(db, title=f"事实新闻{i}", source_id=f"page-{i}")
        _, bad = await _content(db, title="损坏原文", source_id="bad", invalid_hash=True)
        _, future = await _content(db, title="截止后才可见", source_id="future",
                                  received=CUTOFF + timedelta(seconds=1))
        await db.commit()
    cursor, returned, rejected = "", [], 0
    async with env.read_maker() as db:
        while True:
            page = await evidence.read_news_page(db, trade_date=DAY, as_of=CUTOFF,
                                                cursor=cursor, limit=20)
            assert page["total_visible_revision_candidates"] == 58
            assert page["candidate_counts_by_source"] == {"test": 58}
            assert len(page["items"]) <= 20
            assert page["complete_source_collection_certified"] is False
            returned.extend(item["content_version_id"] for item in page["items"])
            rejected += page["page_rejected_integrity_count"]
            cursor = page["next_cursor"]
            if not cursor:
                assert page["stored_window_exhausted"] is True
                break
            with pytest.raises(ValueError, match="exact date and cutoff"):
                await evidence.read_news_page(db, trade_date=DAY,
                        as_of=CUTOFF + timedelta(seconds=1), cursor=cursor)
    assert len(returned) == len(set(returned)) == 57
    assert rejected == 1 and future.id not in returned and bad.id not in returned


@pytest.mark.asyncio
async def test_news_page_latest_invalid_revision_never_borrows_older_good(local_db):
    env = local_db
    async with env.maker() as db:
        await _baseline(db)
        news, old = await _content(db, title="旧事实")
        _, latest = await _content(db, news=news, title="最新损坏", invalid_hash=True)
        await db.commit()
    async with env.read_maker() as db:
        page = await evidence.read_news_page(db, trade_date=DAY, as_of=CUTOFF, limit=1)
    assert page["total_visible_revision_candidates"] == 1
    assert page["items"] == [] and page["page_rejected_integrity_count"] == 1
    assert page["stored_window_exhausted"] is True


@pytest.mark.asyncio
async def test_external_forward_original_is_visible_but_not_session_certificate(local_db):
    env = local_db
    observed = CUTOFF - timedelta(minutes=1)
    fields = {"code": "SPX", "name": "标普500", "price": 5000.0, "previous_close": 5000.0,
              "change_pct": 0.0, "raw_source_time": "unknown_local_clock"}
    payload = {**fields, "kind": "index_quote_observation", "protocol": "global_index_observation_v2_20261008",
               "provider": "akshare:index_global_spot_em", "session": "unknown",
               "quote_clock_certified": False, "collector_observed_at": observed.isoformat(),
               "parser_version": "eastmoney_global_index_fields_v2", "library_version": "fixture",
               "source_timezone": "index_local_unknown", "session_date": None,
               "quote_kind": "index_latest_observation", "us_index_futures_available": False,
               "expected_codes": ["SPX"], "missing_fields": [],
               "response_row_hash": hashlib.sha256(_json(fields).encode()).hexdigest()}
    async with env.maker() as db:
        await _baseline(db)
        _, visible = await _content(db, source="global", title="外盘指数观察",
                     content=_json(payload), publish=observed, received=observed, source_id="global_SPX")
        await _content(db, source="global", title="截止后",
                     content=_json(payload), publish=observed, received=CUTOFF + timedelta(seconds=1),
                     source_id="global_NDX")
        await db.commit()
    result = await _read(env)
    forward = result["external"]["forward_observations"]
    assert forward["status"] == "available" and len(forward["items"]) == 1
    item = forward["items"][0]
    assert item["content_version_id"] == visible.id
    assert item["local_observation_usable_asof"] is True and item["usable_asof"] is False
    assert item["source_quote_at"] is None and item["session_certified"] is False
    assert result["external"]["historical_pit"] is False
    assert result["coverage"]["complete_overnight_coverage"] is False
    assert result["after_hours"]["missing_attribution"] == "stored_observations_missing_collector_outcome_unknown"


@pytest.mark.asyncio
async def test_known_september30_empty_capture_not_confused_with_reader_failure(local_db, monkeypatch):
    env=local_db
    previous,target=date(2026,9,30),date(2026,10,8)
    cutoff=datetime(2026,10,8,8)
    async with env.maker() as db:
        await _baseline(db,previous=previous,target=target,
                        closed=tuple(previous+timedelta(days=i) for i in range(1,8)))
        await db.commit()
    result=await _read(env,target=target,cutoff=cutoff)
    assert result["after_hours"]["missing_attribution"]=="pre_deployment_no_historical_capture"
    async def unavailable(*args,**kwargs):
        raise OSError("isolated missing store")
    monkeypatch.setattr("app.data.after_hours.read_after_hours",unavailable)
    result=await _read(env,target=target,cutoff=cutoff)
    assert result["after_hours"]["status"]=="failed"
    assert "missing_attribution" not in result["after_hours"]


@pytest.mark.asyncio
async def test_normal_explicit_clock_provenance_and_select_only(local_db):
    env = local_db
    async with env.maker() as db:
        post = await _baseline(db)
        _, version = await _content(db)
        attempt = await _analysis(db, version)
        dashboard = DashboardSnapshot(snapshot_key="overview-v2", trade_date=None,
            snapshot_time=CUTOFF - timedelta(minutes=5), status="ok", payload_json=_json({
                "external_factors": [
                    {"key": "us_nasdaq", "label": "纳斯达克", "price": 20000,
                     "change_pct": 1.0, "market": "US", "trade_time": "2026-09-03"},
                ],
            }))
        db.add(dashboard)
        await db.commit()
        before = await db.scalar(select(func.count(NewsContentVersion.id)))
    result = await _read(env)
    assert result["calendar"]["expected_previous"] == PREVIOUS.isoformat()
    assert result["calendar"]["target_status"] == "trade_day"
    assert result["kline"]["actual_kline_date"] == PREVIOUS.isoformat()
    assert result["freshness"]["alignment"] == {
        "kline": "matched", "trusted_kline_anchor": "matched", "postmarket": "matched",
    }
    assert result["previous_postmarket"]["data_version"] == "fixture-data-v1"
    assert result["snapshot_ids"]["previous_postmarket"] == post.id
    assert result["snapshot_ids"]["news_content_versions"] == [version.id]
    assert result["snapshot_ids"]["news_analysis_versions"] == [attempt.id]
    item = result["news"]["items"][0]
    assert item["content_hash"] == version.content_hash
    for field in ("publish_time", "first_received_at", "received_at", "recorded_at",
                  "content_available_at", "analysis_completed_at", "analysis_available_at"):
        assert item[field]
    assert item["id"] == version.news_id
    assert item["title"] == "测试股份签订合同"
    assert item["positive_beneficiary_codes"] == []
    assert item["role_evidence"]["entities"][0]["role"] == "mentioned_entity"
    assert result["news"]["coverage"]["count_complete"] is True
    assert result["news"]["coverage"]["known_zero_news"] is False
    external = result["external"]
    assert external["historical_pit"] is False
    assert external["status"] == "current_projection_only"
    assert external["snapshot_id"] == dashboard.id
    assert external["items"][0]["usable_asof"] is False
    assert external["missing_session_keys"] == ["us_nasdaq"]
    assert "us_sp500" in external["missing_sources"]
    assert external["unavailable_sources"] == ["us_index_futures", "complete_us_after_hours"]
    assert result["readiness"]["baseline_dates_aligned"] is True
    assert result["coverage"]["complete_overnight_coverage"] is False
    assert result["provenance"]["execution_authorized"] is False
    assert env.statements and all(s.lstrip().upper().startswith("SELECT") for s in env.statements)
    async with env.maker() as db:
        assert await db.scalar(select(func.count(NewsContentVersion.id))) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("previous,target,closed,publish", [
    (date(2026, 8, 28), date(2026, 8, 31), (), datetime(2026, 8, 29, 16)),
    (date(2026, 4, 30), date(2026, 5, 6),
     tuple(date(2026, 5, n) for n in range(1, 6)), datetime(2026, 5, 2, 16)),
])
async def test_weekend_and_long_holiday_news_span(local_db, previous, target, closed, publish):
    cutoff = datetime.combine(target, time(8, 55))
    async with local_db.maker() as db:
        await _baseline(db, previous, target, closed=closed)
        await _content(db, publish=publish, source_id="holiday-inside")
        await _content(db, publish=datetime.combine(previous, time(14, 59)), source_id="before-close")
        await _content(db, publish=cutoff + timedelta(minutes=1), source_id="future-publish")
        await db.commit()
    result = await _read(local_db, target=target, cutoff=cutoff)
    assert result["calendar"]["expected_previous"] == previous.isoformat()
    assert result["news"]["window_start"] == datetime.combine(previous, time(15)).isoformat()
    assert result["news"]["count"] == 1
    assert result["news"]["items"][0]["publish_time"] == publish.isoformat()


@pytest.mark.asyncio
async def test_calendar_unknown_does_not_infer_open_or_sync(local_db, monkeypatch):
    async def forbidden(*_args, **_kwargs):
        pytest.fail("calendar auto-sync must never run")
    # Patch the descriptor: restoring an instance bound method would shadow
    # subsequent suites' class-level calendar fixtures.
    monkeypatch.setattr("app.core.trade_calendar.TradeCalendar._ensure_loaded", forbidden)
    async with local_db.maker() as db:
        db.add(StockKline(code="600001", trade_date=PREVIOUS, close=10, source="ths"))
        await db.commit()
    result = await _read(local_db)
    assert result["calendar"]["target_status"] == "calendar_unknown"
    assert result["calendar"]["expected_previous"] is None
    assert result["kline"]["actual_kline_date"] == PREVIOUS.isoformat()
    assert result["news"]["status"] == "unavailable"
    assert result["readiness"]["target_confirmed_trade_day"] is False
    assert result["readiness"]["baseline_dates_aligned"] is False
    assert result["us_reference_session"]["calendar_status"] == "calendar_unknown"
    assert result["us_reference_session"]["venue_open"] is None


@pytest.mark.asyncio
async def test_stored_closed_target_does_not_become_trade_day(local_db):
    async with local_db.maker() as db:
        await _baseline(db, closed=(DAY,))
        await db.commit()
    result = await _read(local_db)
    assert result["calendar"]["target_status"] == "closed"
    assert result["calendar"]["expected_previous"] == PREVIOUS.isoformat()
    assert result["readiness"]["target_confirmed_trade_day"] is False


@pytest.mark.asyncio
async def test_calendar_missing_days_and_stale_kline_are_not_silent_fallback(local_db):
    target, previous = date(2026, 8, 31), date(2026, 8, 28)
    async with local_db.maker() as db:
        db.add_all([TradeCalendarModel(trade_date=previous, is_trade_day=True),
                    TradeCalendarModel(trade_date=target, is_trade_day=True)])
        db.add(StockKline(code="600001", trade_date=previous, close=10, source="ths"))
        await db.commit()
    result = await _read(local_db, target=target, cutoff=datetime.combine(target, time(8, 55)))
    assert result["calendar"]["expected_previous"] is None
    assert result["calendar"]["recorded_previous_candidate"] == previous.isoformat()
    assert result["calendar"]["coverage"]["missing_dates"] == ["2026-08-29", "2026-08-30"]

    # A fully stored calendar must expose that Kline/report stopped two sessions ago.
    async with local_db.maker() as db:
        await _baseline(db, date(2026, 9, 1), DAY)
        await db.commit()
    result = await _read(local_db)
    assert result["calendar"]["expected_previous"] == "2026-09-03"
    assert result["kline"]["actual_kline_date"] == "2026-09-01"
    assert result["freshness"]["alignment"]["kline"] == "stale"
    assert result["freshness"]["alignment"]["postmarket"] == "stale"
    assert result["news"]["window_start"] == "2026-09-01T15:00:00"
    assert result["readiness"]["baseline_dates_aligned"] is False


@pytest.mark.asyncio
async def test_late_receipt_and_analysis_never_backfill_news(local_db):
    async with local_db.maker() as db:
        await _baseline(db)
        _, raw = await _content(db, source_id="raw-with-late-analysis")
        analysis = await _analysis(db, raw, completed=CUTOFF - timedelta(minutes=1),
                                   available=CUTOFF + timedelta(minutes=1))
        _, late = await _content(db, source_id="late-arrival",
                                received=CUTOFF + timedelta(minutes=5))
        await db.commit()
    before = await _read(local_db)
    news = before["news"]
    assert len(news["items"]) == 1
    assert news["items"][0]["analysis_version_id"] is None
    assert news["items"][0]["nlp_missing_at_cutoff"] is True
    assert news["items"][0]["analysis_available_at"] is None
    assert news["positive_count"] == 0
    diag = news["diagnostics"]
    assert diag["received_after_cutoff_revision_count"] == 1
    assert diag["analysis_after_cutoff_attempt_count"] == 1
    assert diag["excluded_metadata"][0]["content_version_id"] == late.id
    assert diag["excluded_metadata"][0]["usable_asof"] is False
    assert "title" not in diag["excluded_metadata"][0]
    after = await _read(local_db, cutoff=CUTOFF + timedelta(minutes=2))
    assert after["news"]["items"][0]["analysis_version_id"] == analysis.id
    assert after["news"]["items"][0]["analysis_available_at"] == analysis.available_at.isoformat()
    assert after["news"]["positive_count"] == 1
    arrival = await _read(local_db, cutoff=CUTOFF + timedelta(minutes=6))
    assert len(arrival["news"]["items"]) == 2
    late_item = next(i for i in arrival["news"]["items"] if i["content_version_id"] == late.id)
    assert late_item["late_collection"] is True


@pytest.mark.asyncio
async def test_revision_isolated_from_old_nlp_and_legacy_remains_unknown(local_db):
    async with local_db.maker() as db:
        await _baseline(db)
        article, original = await _content(db, source_id="revised")
        await _analysis(db, original)
        _, revision = await _content(db, news=article, received=CUTOFF - timedelta(minutes=5),
            title="测试股份终止合同并作风险提示", content="测试股份披露终止合同")
        legacy_article, legacy_old = await _content(db, source_id="legacy")
        await _analysis(db, legacy_old)
        _, legacy = await _content(db, news=legacy_article, origin="legacy_unknown",
                                   received=CUTOFF - timedelta(minutes=10))
        db.add(FinanceNews(source="test", source_id="page-only", title="旧页利好不可倒灌",
                           publish_time=datetime(2026, 9, 3, 17), nlp_status="analyzed",
                           sentiment="positive", related_codes='["600001"]'))
        await db.commit()
    before = await _read(local_db, cutoff=CUTOFF - timedelta(minutes=15))
    assert original.id in before["snapshot_ids"]["news_content_versions"]
    after = await _read(local_db)
    items = after["news"]["items"]
    assert len(items) == 1
    assert items[0]["content_version_id"] == revision.id
    assert items[0]["analysis_version_id"] is None
    assert items[0]["research_only"] is True
    assert items[0]["bull_bear"] == "neutral"
    assert items[0]["positive_beneficiary_codes"] == []
    assert original.content_hash != revision.content_hash
    diag = after["news"]["diagnostics"]
    assert diag["legacy_unknown_revision_count"] == 1
    assert diag["unversioned_projection_article_count"] == 1
    legacy_meta = next(i for i in diag["excluded_metadata"] if i["content_version_id"] == legacy.id)
    assert legacy_meta["reason"] == "legacy_unknown"
    assert legacy_meta["received_at"] is None
    assert legacy_meta["content_available_at"] is None


@pytest.mark.asyncio
async def test_latest_failed_nlp_metadata_retained_without_old_positive_result(local_db):
    async with local_db.maker() as db:
        await _baseline(db)
        _, version = await _content(db)
        await _analysis(db, version)
        failed = await _analysis(db, version, completed=CUTOFF - timedelta(minutes=5), status="failed")
        await db.commit()
    result = await _read(local_db)
    item = result["news"]["items"][0]
    assert item["analysis_version_id"] is None
    assert item["nlp_status"] == "raw"
    assert item["bull_bear"] == "neutral"
    assert item["latest_visible_analysis_attempt"]["analysis_version_id"] == failed.id
    assert item["latest_visible_analysis_attempt"]["status"] == "failed"
    assert item["latest_visible_analysis_attempt"]["used_for_fields"] is False
    assert item["latest_visible_analysis_attempt"]["analysis_available_at"] == failed.available_at.isoformat()


@pytest.mark.asyncio
async def test_bounded_news_has_lower_bound_truncation_and_not_false_total(local_db):
    async with local_db.maker() as db:
        await _baseline(db)
        for n in range(13):
            await _content(db, publish=datetime(2026, 9, 3, 16, n), source_id=f"bounded-{n}")
        await db.commit()
    result = await _read(local_db, limit=2)
    news = result["news"]
    assert news["selected_count"] == len(news["items"]) == 2
    assert news["count"] == 10  # Five bounded pages per requested item, never all rows.
    assert news["coverage"]["visible_count_lower_bound"] == 10
    assert news["coverage"]["count_complete"] is False
    assert news["coverage"]["total_matching_count"] is None
    assert news["coverage"]["reader_truncated_or_unknown"] is True
    assert news["coverage"]["selection_truncated"] is True
    assert news["diagnostics"]["stored_revision_count"] == 13


@pytest.mark.asyncio
async def test_future_or_corrupt_dashboard_never_becomes_asof_pit(local_db):
    async with local_db.maker() as db:
        await _baseline(db)
        db.add(DashboardSnapshot(snapshot_key="overview-v2", trade_date=None, status="ok",
            snapshot_time=CUTOFF + timedelta(hours=1),
            payload_json=_json({"external_factors": [
                {"key": "us_sp500", "price": None, "change_pct": None},
            ]})))
        await db.commit()
    result = await _read(local_db)
    cache = result["external"]
    assert cache["freshness"]["snapshot_after_cutoff"] is True
    assert cache["freshness"]["age_seconds_at_cutoff"] is None
    assert cache["items"][0]["price"] is None
    assert cache["items"][0]["change_pct"] is None
    assert cache["items"][0]["usable_asof"] is False
    async with local_db.writer.begin() as conn:
        await conn.run_sync(DashboardSnapshot.__table__.drop)
    failed = await _read(local_db)
    assert failed["external"]["status"] == "failed"
    assert "external" in failed["coverage"]["failed_components"]
    assert failed["calendar"]["status"] == "available"


@pytest.mark.asyncio
async def test_corrupt_cache_keeps_failed_component_snapshot_reference(local_db):
    async with local_db.maker() as db:
        await _baseline(db)
        row = DashboardSnapshot(snapshot_key="overview-v2", trade_date=None, status="ok",
                                snapshot_time=CUTOFF, payload_json="{invalid-json")
        db.add(row)
        await db.commit()
    result = await _read(local_db)
    assert result["external"]["status"] == "failed"
    assert result["external"]["reason"] == "invalid_external_projection"
    assert result["external"]["snapshot_id"] == row.id
    assert result["snapshot_ids"]["dashboard_latest_projection"] == row.id


@pytest.mark.asyncio
async def test_missing_news_table_is_failed_component_without_ensure(local_db):
    async with local_db.maker() as db:
        await _baseline(db)
        await db.commit()
    async with local_db.writer.begin() as conn:
        await conn.run_sync(NewsContentVersion.__table__.drop)
    result = await _read(local_db)
    assert result["news"]["status"] == "failed"
    assert result["news"]["error_type"] == "OperationalError"
    assert "news" in result["coverage"]["failed_components"]
    assert result["previous_postmarket"]["status"] == "available"
    assert local_db.statements and all(s.lstrip().upper().startswith("SELECT")
                                      for s in local_db.statements)


@pytest.mark.asyncio
async def test_postmarket_future_created_snapshot_is_not_backfilled(local_db):
    async with local_db.maker() as db:
        original = await _baseline(db)
        db.add(DailyReviewSnapshot(review_key="future-created", review_date=PREVIOUS,
            analysis_trade_date=PREVIOUS, phase="postmarket", as_of_at=datetime(2026, 9, 3, 21),
            created_at=CUTOFF + timedelta(minutes=1), data_version="future-repair",
            schema_version="fixture-schema", payload_json="{}"))
        await db.commit()
    result = await _read(local_db)
    assert result["previous_postmarket"]["snapshot_id"] == original.id
    assert result["previous_postmarket"]["data_version"] == "fixture-data-v1"


@pytest.mark.parametrize("cutoff,dst,offset,phase,complete", [
    (datetime(2026, 2, 6, 8, 55), False, -18000, "late_reference_in_progress", False),
    (datetime(2026, 9, 4, 8, 55), True, -14400, "after_late_reference_window", True),
    (datetime(2026, 3, 9, 8, 55), True, -14400, "non_weekday_reference", True),
    (datetime(2026, 11, 2, 8, 55), False, -18000, "non_weekday_reference", False),
    # July 3 could be a holiday; weekday reference is NOT an exchange-open claim.
    (datetime(2026, 7, 4, 8, 55), True, -14400, "after_late_reference_window", True),
])
def test_ny_dst_late_in_progress_and_holiday_calendar_unknown(cutoff, dst, offset, phase, complete):
    context = evidence._us_reference_context(cutoff)
    assert context["dst"] is dst
    assert context["utc_offset_seconds"] == offset
    assert context["reference_phase"] == phase
    assert context["late_reference_window_complete"] is complete
    assert context["calendar_status"] == "calendar_unknown"
    assert context["venue_open"] is None
    if not dst:
        assert "T09:00:00+08:00" in context["late_end_shanghai"]


@pytest.mark.asyncio
async def test_explicit_input_and_dirty_session_guards_prevent_autoflush(local_db):
    async with local_db.read_maker() as db:
        db.add(FinanceNews(source="test", title="禁止隐式flush", publish_time=CUTOFF))
        with pytest.raises(ValueError, match="clean independent"):
            await evidence.read_premarket_context(db, trade_date=DAY, as_of=CUTOFF)
    assert local_db.statements == []
    async with local_db.read_maker() as db:
        for cutoff in (CUTOFF.replace(tzinfo=evidence._SHANGHAI),
                       CUTOFF + timedelta(days=1), CUTOFF.replace(hour=9, minute=30)):
            with pytest.raises(ValueError):
                await evidence.read_premarket_context(db, trade_date=DAY, as_of=cutoff)
        for limit in (0, -1, True, 1.2):
            with pytest.raises(ValueError):
                await evidence.read_premarket_context(db, trade_date=DAY, as_of=CUTOFF, limit=limit)
    assert local_db.statements == []


@pytest.mark.asyncio
async def test_cancellation_not_swallowed_as_partial_context(local_db, monkeypatch):
    async def cancelled(*_args):
        raise asyncio.CancelledError
    monkeypatch.setattr(evidence, "_read_calendar", cancelled)
    with pytest.raises(asyncio.CancelledError):
        await _read(local_db)


@pytest.mark.asyncio
async def test_review_local_news_payload_compatible_and_spans_holiday(local_db):
    previous, target = date(2026, 4, 30), date(2026, 5, 6)
    cutoff = datetime.combine(target, time(8, 55))
    async with local_db.maker() as db:
        await _baseline(db, previous, target, closed=tuple(date(2026, 5, n) for n in range(1, 6)))
        _, version = await _content(db, publish=datetime(2026, 5, 2, 16), source_id="holiday-review")
        await _analysis(db, version, completed=datetime(2026, 5, 2, 18))
        await db.commit()
    async with local_db.read_maker() as db:
        dimensions, _ = await _market_dimensions(
            db, analysis_trade_date=previous, review_date=target, phase="premarket",
            as_of_at=cutoff, plan_snapshots=[],
        )
    news = dimensions["news"]
    assert news["window_start"] == "2026-04-30T15:00:00"
    assert news["count_scope"] == "as_of_window"
    assert news["count"] == news["selected_count"] == news["positive_count"] == 1
    assert news["items"][0]["content_version_id"] == version.id
    assert {"id", "publish_time", "source", "title", "summary", "importance", "category",
            "nlp_status", "bull_bear", "confidence", "related_codes", "related_sectors",
            "impact_reason", "impact_scope", "has_specific_target"} <= news["items"][0].keys()


def _fallback(monkeypatch, rows):
    collector = ExternalFactorCollector()
    monkeypatch.setattr(collector, "_parse_tencent_quote", lambda _code: None)
    monkeypatch.setattr("app.dashboard2.external_sources.ak.index_us_stock_sina",
                        lambda symbol: pd.DataFrame(rows))
    return collector._collect_us_indices()


def test_us_fallback_adjacent_valid_date_prior_close_not_open(monkeypatch):
    items = _fallback(monkeypatch, [
        {"date": "2026-09-03", "open": 999, "close": 110},
        {"date": "2026-09-01", "open": 777, "close": 100},
        {"date": "2026-09-02", "open": 800, "close": None},
        {"date": "bad-date", "close": 150},
        {"date": "2026-09-03", "open": 50, "close": 110},
    ])
    assert len(items) == 2
    assert {i.key for i in items} == {"us_nasdaq", "us_sp500"}
    assert all(i.change_pct == 10 and i.price == 110 and i.trade_time == "2026-09-03" for i in items)


@pytest.mark.parametrize("rows", [
    [{"date": "2026-09-03", "open": 90, "close": 100}],
    [{"date": "2026-09-02", "close": 0}, {"date": "2026-09-03", "close": 100}],
    [{"date": "2026-09-02", "close": float("inf")}, {"date": "2026-09-03", "close": 100}],
    [{"date": "2026-09-02", "close": 1e-308}, {"date": "2026-09-03", "close": 1e308}],
    [{"date": "2026-09-02", "close": 95}, {"date": "2026-09-02", "close": 96},
     {"date": "2026-09-03", "close": 100}],
])
def test_us_fallback_without_prior_close_unavailable_not_zero(monkeypatch, rows):
    assert _fallback(monkeypatch, rows) == []


def test_us_fallback_does_not_borrow_cross_symbol_prior_close(monkeypatch):
    assert _fallback(monkeypatch, [
        {"symbol": ".IXIC", "date": "2026-09-03", "close": 110},
        {"symbol": ".INX", "date": "2026-09-02", "close": 100},
    ]) == []


def test_us_fallback_order_independent_and_open_optional(monkeypatch):
    rows = [{"date": "2026-09-01", "close": 100},
            {"date": "2026-09-02", "close": 105},
            {"date": "2026-09-03", "close": 110}]
    first = [i.model_dump() for i in _fallback(monkeypatch, rows)]
    second = [i.model_dump() for i in _fallback(monkeypatch, list(reversed(rows)))]
    assert first == second
    assert first[0]["change_pct"] == 4.76


def test_no_main_import():
    # Source-level dependency assertion also works in suites where unrelated
    # tests imported main before collection; this test never imports/runs it.
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(evidence))
    imports = [node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
    assert "app.main" not in imports


def _frozen_stats(previous=PREVIOUS):
    day = previous.isoformat()
    return {"dimensions": {
        "fundamental": {"status": "daily_snapshot", "source_trade_date": day,
            "candidate_count": 2, "requested_candidate_count": 3, "coverage_ratio": 2/3,
            "valid_pe_count": 2, "valid_pb_count": 1, "valid_growth_count": 2,
            "median_pe_ttm": 12.5, "median_pb": 1.2, "median_net_profit_growth": -3},
        "technical": {"source": "stock_kline_close", "source_trade_date": day,
            "technical_trade_date": day, "universe_count": 100, "change_coverage": .8,
            "advance_ratio": .4, "median_return": -1, "limit_up_count": 5,
            "limit_down_count": 2, "broken_limit_count": 1, "seal_rate": .8, "board_height": 3},
        "capital": {"source_trade_date": day, "analysis_trade_date": day,
            "market_main_net_inflow": -50, "sampled_stock_main_net_inflow": 10,
            "sampled_stock_count": 2, "fund_flow_record_count": 5, "sample_basis": "top_main_net_inflow_30"},
    }}


def _review(payload, *, suffix="stats", as_of=None, created=None):
    as_of = as_of or datetime.combine(PREVIOUS, time(21))
    return DailyReviewSnapshot(review_key=suffix, review_date=PREVIOUS, analysis_trade_date=PREVIOUS,
        phase="postmarket", as_of_at=as_of, created_at=created or as_of,
        data_version="frozen.stats.v1", schema_version="fixture", quality_status="partial",
        payload_json=payload if isinstance(payload, str) else _json(payload))


@pytest.mark.asyncio
async def test_fusion_reports_separate_frozen_stats_and_visible_news_without_zero_fill(local_db):
    async with local_db.maker() as db:
        await _baseline(db)
        post = _review(_frozen_stats())
        db.add(post)
        _, content = await _content(db)
        await _analysis(db, content)
        await _content(db, source_id="too-late-for-summary", received=CUTOFF+timedelta(minutes=1))
        await db.commit()
    result = await _read(local_db)
    summary = result["research_fusion"]["statistics"]
    dims = summary["dimensions"]
    assert dims["fundamental"]["statistics"]["median_pe_ttm"] == 12.5
    assert dims["technical"]["statistics"]["median_return"] == -1
    assert dims["capital"]["statistics"]["market_main_net_inflow"] == -50
    assert dims["news"]["statistics"]["count"] == dims["news"]["statistics"]["positive_count"] == 1
    assert not dims["news"]["coverage"]["known_zero_news"]
    assert dims["after_hours"]["statistics"]["after_volume_ratio"]["median"] is None
    assert dims["fundamental"]["snapshot_id"] == post.id
    assert not dims["fundamental"]["individual_input_availability_certified"]
    assert not summary["automatic_weight_update"] and not summary["execution_authorized"]


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["wrong_day", "missing_day", "unavailable", "failed", "invalid",
                                  "boolean_count", "huge_integer"])
async def test_fusion_never_upgrades_invalid_dimension_to_available_stats(local_db, damage):
    payload = _frozen_stats()
    value = payload["dimensions"]["fundamental"]
    if damage == "wrong_day":
        value["source_trade_date"] = (PREVIOUS-timedelta(days=1)).isoformat()
    elif damage == "missing_day":
        value.pop("source_trade_date")
    elif damage in {"unavailable", "failed", "invalid"}:
        value["status"] = damage
    else:
        value["candidate_count"] = True if damage == "boolean_count" else 10**1000
    async with local_db.maker() as db:
        await _baseline(db)
        db.add(_review(payload))
        await db.commit()
    result = await _read(local_db)
    fundamental = result["research_fusion"]["statistics"]["dimensions"]["fundamental"]
    assert fundamental["status"] != "available"
    if damage in {"wrong_day", "missing_day", "unavailable", "failed", "invalid"}:
        assert all(number is None for number in fundamental["statistics"].values())
    else:
        assert fundamental["statistics"]["candidate_count"] is None
    assert result["research_fusion"]["statistics"]["dimensions"]["technical"]["status"] == "available"


@pytest.mark.asyncio
@pytest.mark.parametrize("extra_bytes", [0, 1])
async def test_snapshot_payload_sql_gate_uses_utf8_bytes_and_never_falls_back(local_db, monkeypatch, extra_bytes):
    monkeypatch.setattr(evidence, "_MAX_SNAPSHOT_BYTES", 256)
    raw = _json({"dimensions": {"fundamental": {"note": "中"*40}}, "padding": ""})
    raw = raw[:-2] + "x" * (256 - len(raw.encode()) + extra_bytes) + raw[-2:]
    assert len(raw.encode()) == 256 + extra_bytes
    async with local_db.maker() as db:
        old = await _baseline(db)
        latest = _review(raw)
        db.add(latest)
        await db.commit()
    selected_payloads = []
    original_execute = AsyncSession.execute

    async def capture_projection(session, statement, *args, **kwargs):
        result = await original_execute(session, statement, *args, **kwargs)
        if "payload_byte_count" not in str(statement):
            return result
        def one_or_none():
            row = result.one_or_none()
            selected_payloads.append(row.payload_json)
            return row
        return SimpleNamespace(one_or_none=one_or_none)

    monkeypatch.setattr(AsyncSession, "execute", capture_projection)
    result = await _read(local_db)
    post = result["previous_postmarket"]
    assert selected_payloads == [None if extra_bytes else raw]
    assert post["snapshot_id"] == latest.id != old.id
    assert post["payload_byte_count"] == 256 + extra_bytes
    assert post["status"] == ("invalid" if extra_bytes else "available")
    assert post["payload_hash"] is None if extra_bytes else len(post["payload_hash"]) == 64
    if extra_bytes:
        assert post["dimensions"]["reason"] == "snapshot_payload_byte_budget"
        assert result["readiness"]["baseline_dates_aligned"] is False
    sql = next(statement for statement in local_db.statements if "payload_byte_count" in statement)
    assert "CASE WHEN" in sql and "CAST(" in sql and "BLOB" in sql
    # Selected leaves only, not ORM-loading the whole snapshot.
    assert "market_regime" not in sql and "quality_score" not in sql


@pytest.mark.asyncio
async def test_snapshot_created_before_its_own_asof_is_not_fusion_evidence(local_db):
    async with local_db.maker() as db:
        await _baseline(db)
        db.add(_review(_frozen_stats(), created=datetime.combine(PREVIOUS, time(20))))
        await db.commit()
    result = await _read(local_db)
    assert result["previous_postmarket"]["status"] == "invalid"
    stats = result["research_fusion"]["statistics"]["dimensions"]
    assert all(stats[name]["status"] == "unavailable" for name in ("fundamental", "technical", "capital"))


def test_after_hours_summary_is_bounded_sample_not_full_market_or_filled_volume():
    post = {"status": "unavailable"}
    news = {"status": "unavailable"}
    after = {"source": "fixture", "trade_date": PREVIOUS.isoformat(), "stored_code_count": 1000,
             "truncated": True, "items": [
                 {"after_volume_ratio": .1, "after_amount_ratio": .2},
                 {"after_volume_ratio": None, "after_amount_ratio": .4},
                 {"after_volume_ratio": False, "after_amount_ratio": 10**1000},
             ]}
    result = evidence._fusion_statistics(post, news, after, expected=PREVIOUS, as_of=CUTOFF)
    summary = result["dimensions"]["after_hours"]
    assert summary["statistics"]["after_volume_ratio"] == {"valid_count": 1, "missing_count": 2, "median": .1}
    assert summary["statistics"]["after_amount_ratio"]["valid_count"] == 2
    assert summary["statistics"]["after_amount_ratio"]["median"] == pytest.approx(.3)
    assert summary["stored_code_count"] == 1000 and summary["returned_code_count"] == 3
    assert summary["truncated"] and not summary["complete_market_coverage"] and not summary["trading_authority"]


@pytest.mark.asyncio
async def test_latest_visible_bad_analysis_date_is_invalid_not_an_old_snapshot_fallback(local_db):
    async with local_db.maker() as db:
        old = await _baseline(db)
        latest = _review(_frozen_stats())
        latest.analysis_trade_date = DAY
        db.add(latest)
        await db.commit()
    result = await _read(local_db)
    post = result["previous_postmarket"]
    assert post["snapshot_id"] == latest.id != old.id
    assert post["status"] == "invalid"
    assert post["analysis_trade_date"] == DAY.isoformat()
    assert result["readiness"]["baseline_dates_aligned"] is False
    dims = result["research_fusion"]["statistics"]["dimensions"]
    assert all(dims[name]["status"] == "unavailable" for name in ("fundamental", "technical", "capital"))


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["data_version", "schema_version", "quality_status"])
@pytest.mark.parametrize("value", ["", " \t\n"])
async def test_latest_snapshot_missing_required_metadata_is_invalid_not_fallback(local_db, field, value):
    async with local_db.maker() as db:
        old = await _baseline(db)
        latest = _review(_frozen_stats())
        setattr(latest, field, value)
        db.add(latest)
        await db.commit()
    result = await _read(local_db)
    post = result["previous_postmarket"]
    assert post["snapshot_id"] == latest.id != old.id
    assert post["status"] == "invalid"
    assert result["readiness"]["baseline_dates_aligned"] is False
    dims = result["research_fusion"]["statistics"]["dimensions"]
    assert all(dims[name]["status"] == "unavailable" for name in ("fundamental", "technical", "capital"))


@pytest.mark.asyncio
async def test_eight_am_after_holiday_fuses_prior_stats_and_only_then_visible_news(local_db, monkeypatch):
    # Explicit future test clocks, not a claim that these observations already exist.
    previous, target = date(2026, 9, 30), date(2026, 10, 8)
    cutoff = datetime.combine(target, time(8))
    closed = tuple(previous + timedelta(days=i) for i in range(1, 8))
    async with local_db.maker() as db:
        await _baseline(db, previous, target, closed=closed)
        stamp = datetime.combine(previous, time(20, 35))
        post = _review(_frozen_stats(previous), as_of=stamp, created=stamp)
        post.review_date = post.analysis_trade_date = previous
        db.add(post)
        _, content = await _content(db, publish=datetime(2026, 10, 7, 16), source_id="holiday-fusion")
        await _analysis(db, content, completed=datetime(2026, 10, 7, 20))
        await _content(db, publish=cutoff - timedelta(minutes=10),
                       received=cutoff + timedelta(minutes=1), source_id="received-after-eight")
        await db.commit()
    # Both readers cap future cutoffs at real now; advance only the isolated
    # test clocks, preserving that cap and all ordinary datetime type checks.
    from app.news import catalyst
    class ClockType(type):
        def __instancecheck__(cls, value):
            return isinstance(value, datetime)
    class TestClock(datetime, metaclass=ClockType):
        @classmethod
        def now(cls, tz=None):
            instant = cutoff.replace(tzinfo=evidence._SHANGHAI)
            return instant.astimezone(tz) if tz else cutoff
    monkeypatch.setattr(evidence, "datetime", TestClock)
    monkeypatch.setattr(catalyst, "datetime", TestClock)
    result = await _read(local_db, target=target, cutoff=cutoff)
    fusion = result["research_fusion"]
    stats = fusion["statistics"]
    assert stats["as_of_at"] == cutoff.isoformat()
    assert stats["expected_previous"] == previous.isoformat()
    assert result["readiness"]["baseline_dates_aligned"]
    assert all(stats["dimensions"][name]["status"] == "available"
               for name in ("fundamental", "technical", "capital"))
    assert stats["dimensions"]["news"]["statistics"]["count"] == 1
    assert stats["dimensions"]["news"]["statistics"]["positive_count"] == 1
    assert stats["dimensions"]["news"]["window_start"] == datetime.combine(previous, time(15)).isoformat()
    assert fusion["same_day_capture_retained"] and not fusion["recomputed_previous_day_from_current_spot"]
    assert not stats["execution_authorized"] and not stats["automatic_weight_update"]
