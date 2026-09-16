"""晋级消息consumer复用直连证据，不旁路查询可变新闻/板块。"""

from copy import deepcopy
from datetime import date, datetime

import pytest

from app.api.v1 import promotion


@pytest.mark.asyncio
async def test_news_adapter_preserves_evidence_and_cutoff_without_mutating_shared_map(monkeypatch):
    cutoff = datetime(2026, 9, 14, 9, 25)
    evidence = {
        "000888": {
            "news_catalyst_score": 60.0,
            "news_title": "企业重大合同",
            "news_count": 1,
            "news_evidence": {"protocol": "news_pit_v1", "versions": [{"content_version_id": 7, "analysis_version_id": 9}]},
            "news_titles": ["企业重大合同"],
        },
        "000887": {"news_catalyst_score": 80.0},
    }
    before = deepcopy(evidence)
    calls = []

    class NoDatabaseQueries:
        async def execute(self, *_args, **_kwargs):
            raise AssertionError("直连loader之外不能另查可变新闻/板块")

    db = NoDatabaseQueries()

    async def shared_loader(session, trade_date, **kwargs):
        assert session is db
        assert trade_date == date(2026, 9, 14)
        calls.append(kwargs)
        return evidence

    monkeypatch.setattr(promotion, "load_direct_stock_catalyst_map", shared_loader)
    result = await promotion._load_news_catalyst_context_map(
        db, date(2026, 9, 14), exclude_codes={"000887"},
        limit=300, news_end_time=cutoff,
    )
    assert set(result) == {"000888"}
    assert result["000888"]["news_mapping_mode"] == "direct_code"
    assert result["000888"]["news_sector_inferred"] is False
    assert result["000888"]["news_evidence"] == evidence["000888"]["news_evidence"]
    assert calls == [{
        "limit": 600, "min_score": promotion.NEWS_CATALYST_MIN_SCORE,
        "news_end_time": cutoff,
    }]
    assert evidence == before


@pytest.mark.asyncio
async def test_news_adapter_does_not_fallback_when_evidence_loader_fails(monkeypatch):
    class NoDatabaseQueries:
        async def execute(self, *_args, **_kwargs):
            raise AssertionError("不能回退到旧新闻表")

    async def failed_loader(*_args, **_kwargs):
        raise RuntimeError("immutable evidence unavailable")

    monkeypatch.setattr(promotion, "load_direct_stock_catalyst_map", failed_loader)
    with pytest.raises(RuntimeError, match="immutable evidence unavailable"):
        await promotion._load_news_catalyst_context_map(
            NoDatabaseQueries(), date(2026, 9, 14), exclude_codes=set(),
        )


def test_prediction_factor_and_reason_keep_owned_news_evidence_without_changing_score():
    evidence = {
        "protocol": "news_pit_v1",
        "versions": [{"content_version_id": 7, "analysis_version_id": 9}],
        "as_of_at": "2026-09-14T09:25:00",
    }
    row = {
        "event_types": ["news_catalyst"],
        "detail": {"news_catalyst_score": 69, "news_evidence": evidence},
    }
    baseline_row = deepcopy(row)
    baseline_row["detail"].pop("news_evidence")
    baseline_probability, baseline_factors = promotion._build_first_board_probability(
        baseline_row, {}, {}, {},
    )
    probability, factors = promotion._build_first_board_probability(row, {}, {}, {})
    assert probability == baseline_probability
    assert baseline_factors["news_evidence"] == {}
    assert factors["news_evidence"] == evidence
    assert factors["news_evidence"] is not evidence
    reason = promotion._build_prediction_reason_snapshot({"probability_factors": factors})
    assert reason["news_evidence"] == evidence
    factors["news_evidence"]["versions"][0]["content_version_id"] = 99
    assert evidence["versions"][0]["content_version_id"] == 7
    assert reason["news_evidence"]["versions"][0]["content_version_id"] == 7
    assert promotion._build_prediction_reason_snapshot({})["news_evidence"] == {}


def test_radar_dragon_evidence_is_explanatory_owned_and_not_a_score_change():
    from app.api.v1.tenbagger import _serialize_dragon_result
    from app.signal.dragon_head import DragonHeadScanner

    evidence = {"protocol": "news_pit_v1", "versions": [{"content_version_id": 7}]}
    stock = {
        "code": "000888", "name": "测试公司", "change_pct": 5.0,
        "event_grade": "hard", "event_score": 69.0, "news_evidence": evidence,
    }
    scanner = DragonHeadScanner()
    baseline = scanner.scan_sector("S_TEST", "测试板块", [
        {key: value for key, value in stock.items() if key != "news_evidence"}
    ])[0]
    result = scanner.scan_sector("S_TEST", "测试板块", [stock])[0]
    assert (result.score, result.level, result.tradability_score) == (
        baseline.score, baseline.level, baseline.tradability_score,
    )
    assert result.news_evidence == evidence
    assert baseline.news_evidence == {}
    payload = _serialize_dragon_result(result)
    assert payload["news_evidence"] == evidence
    result.news_evidence["versions"][0]["content_version_id"] = 99
    assert payload["news_evidence"]["versions"][0]["content_version_id"] == 7
    assert evidence["versions"][0]["content_version_id"] == 7


@pytest.mark.asyncio
async def test_real_news_revision_reaches_promotion_and_radar_without_late_analysis(monkeypatch):
    from datetime import timedelta

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.api.v1.tenbagger import _enrich_plan_candidates_with_direct_catalysts
    from app.models.news import FinanceNews, NewsAnalysisVersion, NewsContentVersion
    from app.models.stock import StockSpot
    from app.news.engine import NewsEngine
    from app.news.sources.base import NewsItem

    at = datetime(2026, 5, 20, 9, 20)
    clock = [at]
    monkeypatch.setattr("app.news.engine._news_now", lambda: clock[0])
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            for model in (FinanceNews, StockSpot, NewsContentVersion, NewsAnalysisVersion):
                await conn.run_sync(lambda sync, m=model: m.__table__.create(sync))
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            db.add(StockSpot(code="000888", name="测试科技"))
            await db.commit()
            news_engine = NewsEngine()
            raw = NewsItem(
                source="cninfo", source_id="integration-news",
                title="测试科技重大资产重组获批", content="测试科技公告。",
                publish_time=at - timedelta(hours=1),
            )
            await news_engine.cache_raw_items(db, [raw])
            early = await promotion._load_news_catalyst_context_map(
                db, at.date(), exclude_codes=set(), news_end_time=at - timedelta(microseconds=1),
            )
            assert early == {}
            visible = await promotion._load_news_catalyst_context_map(
                db, at.date(), exclude_codes=set(), news_end_time=at,
            )
            assert set(visible) == {"000888"}
            assert visible["000888"]["news_evidence"]
            assert visible["000888"]["news_analysis_version_id"] is None
            clock[0] += timedelta(hours=1)
            await news_engine._save_to_db(db, raw, {
                "sentiment": "bullish", "confidence": 0.9, "importance": 8,
                "impact_scope": "stock", "sentiment_method": "ai", "events_method": "ai",
                "related_codes": ["000887"], "related_sectors": [], "events": [],
            })
            await db.commit()
            replay = await promotion._load_news_catalyst_context_map(
                db, at.date(), exclude_codes=set(), news_end_time=at,
            )
            assert replay == visible
            current = await promotion._load_news_catalyst_context_map(
                db, at.date(), exclude_codes=set(), news_end_time=clock[0],
            )
            assert set(current) == {"000888"}  # AI's unsupported code cannot leak.
            assert current["000888"]["news_analysis_version_id"] is not None
            radar = _enrich_plan_candidates_with_direct_catalysts(
                [{"code": "000888", "main_wave_stats": {}}], current,
            )
            _, factors = promotion._build_first_board_probability(
                {"detail": current["000888"], "event_types": ["news_catalyst"]},
                {}, {}, {},
            )
            assert radar[0]["main_wave_stats"]["news_evidence"] == factors["news_evidence"]
    finally:
        await engine.dispose()
