from datetime import date, datetime, time, timedelta
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.v1 import daily_review
from app.db.session import Base, get_db
from app.models.news import FinanceNews
from app.models.promotion import PromotionPredictionRun
from app.models.review import (
    DailyReviewSnapshot,
    PromotionReviewAttribution,
    ReviewAutomationAlert,
    ReviewAutomationRun,
)
from app.models.sector import SectorLifecycle
from app.models.stock import (
    FundFlow,
    LimitUpPool,
    MarketSentiment,
    SectorPersistence,
    StockFundamentalDaily,
    StockKline,
    StockSpot,
    StockTag,
)
from app.promotion.ledger import append_prediction_run
from app.promotion.versioning import PromotionModelIdentity, PromotionRuntimeMode
from app.review.automation import replay_daily_reviews, run_review_automation
from app.review.gpt_report import GptReportDisabledError, generate_gpt_report
from app.review.service import (
    _market_dimensions,
    build_daily_review_snapshot,
    build_review_views,
)


@pytest_asyncio.fixture
async def review_env(tmp_path: Path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'review.db'}", future=True
    )
    SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    app = FastAPI()
    app.include_router(daily_review.router, prefix="/api/v1/daily-review")

    async def override_get_db():
        async with SessionLocal() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield SessionLocal, client
    app.dependency_overrides.clear()
    await engine.dispose()


async def _seed_review_data(session: AsyncSession) -> None:
    prediction_day = date(2026, 8, 27)
    outcome_day = date(2026, 8, 28)
    codes = [f"000{index:03d}" for index in range(20)] + ["000099"]
    for day_index, trade_day in enumerate((prediction_day, outcome_day)):
        for index, code in enumerate(codes):
            change = 2.0 if (index + day_index) % 3 else -1.0
            session.add(
                StockKline(
                    code=code,
                    trade_date=trade_day,
                    open=10,
                    high=11,
                    low=9.8,
                    close=10.2,
                    prev_close=10,
                    change_pct=change,
                    volume=1000,
                    amount=1_000_000 + index * 1000,
                    turnover=3 + index * 0.1,
                )
            )
    session.add(
        MarketSentiment(
            trade_date=outcome_day,
            sentiment_cycle="recovery",
            limit_up_count=4,
            limit_down_count=2,
            broken_limit_count=3,
            seal_rate=72,
            board_height=2,
            turnover_total=1.2345,
            main_net_inflow=20,
            quality_status="ok",
            quality_reason="",
        )
    )
    session.add(
        FundFlow(
            code="000001",
            name="命中股",
            trade_date=outcome_day,
            main_net_inflow=10_000_000,
            main_net_inflow_pct=8,
        )
    )
    session.add(
        SectorPersistence(
            sector_code="S1",
            sector_name="测试主线",
            trade_date=outcome_day,
            consecutive_days=3,
            limit_up_count=3,
            fund_flow=8,
            change_pct=2.5,
            strength_score=85,
        )
    )
    session.add(
        SectorLifecycle(
            trade_date=outcome_day,
            sector_code="S1",
            sector_name="测试主线",
            sector_type="concept",
            lifecycle_state="accelerating",
            state_score=88,
            quality_score=80,
            max_board_height=2,
            is_main_line=1,
        )
    )
    session.add(
        FinanceNews(
            source="test",
            source_id="review-news-1",
            title="测试政策消息",
            publish_time=datetime(2026, 8, 28, 9, 0),
            importance=8,
            sentiment="positive",
            bull_bear="bull",
            bull_bear_confidence=0.8,
            nlp_status="analyzed",
            related_codes='["000001"]',
            related_sectors='["测试主线"]',
        )
    )
    for code, name, boards in (
        ("000001", "命中股", 1),
        ("000009", "排序漏股", 1),
        ("000099", "召回漏股", 1),
        ("000010", "二板命中", 2),
    ):
        session.add(
            LimitUpPool(
                code=code,
                name=name,
                trade_date=outcome_day,
                consecutive_days=boards,
                limit_up_reason="测试主线",
                source="test",
            )
        )
    await session.commit()

    candidates = []
    for index in range(10):
        code = f"000{index:03d}"
        ranked = index < 3
        candidates.append(
            {
                "code": code,
                "name": f"首板{index}",
                "target_board": 1,
                "candidate_route": "pre_board_probe_start",
                "probability": 0.3 - index * 0.01,
                "probability_factors": {
                    "prediction_snapshot_source": "schedule",
                    "prediction_snapshot_context": "promotion_2000",
                    "prediction_snapshot_recorded_at": "2026-08-27T20:00:00",
                    "prediction_snapshot_batch_key": "review-test-20260827",
                    "prediction_record_scope": "ranked" if ranked else "pool_unranked",
                    "prediction_ranked_selected": ranked,
                    "prediction_ranked_position": index + 1 if ranked else None,
                    "prediction_pool_rank": index + 1,
                    "prediction_actionable": index == 0,
                },
            }
        )
    for index, code in enumerate(("000010", "000011", "000012")):
        ranked = index < 2
        candidates.append(
            {
                "code": code,
                "name": f"二板{index}",
                "target_board": 2,
                "candidate_route": "second_board_promotion",
                "probability": 0.25 - index * 0.02,
                "probability_factors": {
                    "prediction_snapshot_source": "schedule",
                    "prediction_snapshot_context": "promotion_2000",
                    "prediction_snapshot_recorded_at": "2026-08-27T20:00:00",
                    "prediction_snapshot_batch_key": "review-test-20260827",
                    "prediction_record_scope": "ranked" if ranked else "pool_unranked",
                    "prediction_ranked_selected": ranked,
                    "prediction_ranked_position": index + 1 if ranked else None,
                    "prediction_pool_rank": index + 1,
                    "prediction_actionable": index == 0,
                },
            }
        )
    identity = PromotionModelIdentity(
        runtime_mode=PromotionRuntimeMode.LEGACY,
        champion_model_version="review_champion_v1",
        challenger_model_version=None,
        feature_version="review_features_v1",
        data_version="review_data_v1",
    )
    await append_prediction_run(
        session,
        candidates,
        {1: prediction_day, 2: prediction_day},
        identity=identity,
        snapshot_source="schedule",
        snapshot_context="promotion_2000",
        quality_gate={"gate_passed": True},
    )
    await session.commit()


@pytest.mark.asyncio
async def test_postmarket_review_freezes_scorecard_and_structural_attributions(review_env):
    SessionLocal, client = review_env
    async with SessionLocal() as session:
        await _seed_review_data(session)
        first = await build_daily_review_snapshot(
            session,
            review_date=date(2026, 8, 28),
            phase="postmarket",
            persist=True,
        )
        await session.commit()
        second = await build_daily_review_snapshot(
            session,
            review_date=date(2026, 8, 28),
            phase="postmarket",
            persist=True,
        )
        await session.commit()
        snapshot_count = await session.scalar(select(func.count()).select_from(DailyReviewSnapshot))
        attribution_count = await session.scalar(select(func.count()).select_from(PromotionReviewAttribution))
        run_count = await session.scalar(select(func.count()).select_from(PromotionPredictionRun))

    assert first["review_key"] == second["review_key"]
    assert snapshot_count == 1
    assert run_count == 1
    assert attribution_count == first["prediction_review"]["attribution_count"]
    target1 = first["prediction_review"]["targets"]["1"]
    assert target1["snapshot_complete"] is True
    assert target1["pool_hit_count"] == 2
    assert target1["ranked_hit_count"] == 1
    assert target1["not_in_pool_count"] == 1
    assert {row["attribution_type"] for row in first["attributions"]} >= {
        "hit",
        "ranking_miss",
        "recall_miss",
        "false_positive",
    }
    assert all("claim_boundary" in row["evidence"] for row in first["attributions"])

    detail = await client.get(f"/api/v1/daily-review/snapshots/{first['id']}")
    assert detail.status_code == 200
    payload = detail.json()
    assert payload["dimensions"]["news"]["count"] == 1
    assert payload["dimensions"]["news"]["count_scope"] == "as_of_window"
    assert payload["dimensions"]["news"]["selected_count"] == 1
    assert payload["dimensions"]["capital"]["sample_basis"] == "top_main_net_inflow_30"
    assert payload["dimensions"]["capital"]["sampled_stock_count"] == 1
    assert payload["dimensions"]["technical"]["turnover_total_trillion"] == pytest.approx(1.2345)
    assert payload["dimensions"]["technical"]["total_amount"] == pytest.approx(1.2345e12)
    assert payload["dimensions"]["technical"]["total_amount_source"] == "market_sentiment_index_aggregate"
    assert payload["dimensions"]["limit_up_learning"]["count"] == 4
    iteration_sample = payload["dimensions"]["limit_up_learning"][
        "strategy_iteration_sample"
    ]
    assert iteration_sample["sample_version"] == "abcdef_daily_outcome_v1"
    assert iteration_sample["eligible_for_threshold_tuning"] is False
    assert iteration_sample["coverage"]["kline_complete_count"] == 4
    assert iteration_sample["opening_bucket_counts"]["zero_axis_-1_to_1"] == 4
    assert len(iteration_sample["outcomes"]) == 4
    assert isinstance(payload["market_regime"], dict)
    assert payload["market_regime_code"] == payload["market_regime"]["primary_regime"]
    assert payload["guardrails"]
    assert payload["schema_version"] == "daily_review_workbench_v5"
    assert payload["view_schema_version"] == "daily_review_decision_view_v3"
    assert payload["next_trade_date"] == "2026-08-31"
    assert payload["review_conclusion"]["headline"]
    assert payload["review_conclusion"]["evidence"]
    assert payload["next_session_guide"]["trade_date"] == "2026-08-31"
    assert len(payload["next_session_guide"]["playbook"]) == 3
    assert len(payload["attributions"]) == attribution_count

    note = await client.post(
        "/api/v1/daily-review/notes",
        json={
            "review_date": "2026-08-28",
            "phase": "postmarket",
            "category": "hypothesis",
            "content": "验证排序漏股是否集中在板块主升切片",
            "tags": ["排序", "风格"],
        },
    )
    assert note.status_code == 200
    notes = await client.get(
        "/api/v1/daily-review/notes",
        params={"review_date": "2026-08-28", "phase": "postmarket"},
    )
    assert notes.json()["count"] == 1


@pytest.mark.asyncio
async def test_incomplete_prediction_snapshot_never_claims_not_in_pool(review_env):
    SessionLocal, _client = review_env
    async with SessionLocal() as session:
        await _seed_review_data(session)
        later_day = date(2026, 8, 31)
        for index in range(12):
            session.add(
                StockKline(
                    code=f"001{index:03d}",
                    trade_date=later_day,
                    open=10,
                    high=11,
                    low=9.8,
                    close=10.5,
                    prev_close=10,
                    change_pct=2,
                    volume=1000,
                )
            )
        session.add(
            LimitUpPool(
                code="001000",
                name="无快照真实涨停",
                trade_date=later_day,
                consecutive_days=1,
                source="test",
            )
        )
        await session.commit()
        result = await build_daily_review_snapshot(
            session,
            review_date=later_day,
            phase="postmarket",
            persist=False,
        )

    assert result["prediction_review"]["status"] == "snapshot_incomplete"
    assert result["prediction_review"]["targets"]["1"]["not_in_pool_count"] is None
    assert result["attributions"] == []


@pytest.mark.asyncio
async def test_review_automation_is_idempotent_alerts_on_degraded_quality_and_replays(review_env):
    SessionLocal, client = review_env
    async with SessionLocal() as session:
        await _seed_review_data(session)
        completed = await run_review_automation(
            session,
            review_date=date(2026, 8, 28),
            phase="postmarket",
            retry_attempts=1,
            retry_delay_seconds=0,
        )
        duplicate = await run_review_automation(
            session,
            review_date=date(2026, 8, 28),
            phase="postmarket",
            retry_attempts=1,
            retry_delay_seconds=0,
        )
        degraded = await run_review_automation(
            session,
            review_date=date(2026, 8, 28),
            phase="premarket",
            retry_attempts=1,
            retry_delay_seconds=0,
        )
        run_count = await session.scalar(select(func.count()).select_from(ReviewAutomationRun))
        alert_count = await session.scalar(select(func.count()).select_from(ReviewAutomationAlert))
        replay = await replay_daily_reviews(
            session,
            start_date=date(2026, 8, 27),
            end_date=date(2026, 8, 28),
            phases=("postmarket",),
            persist=False,
        )

    assert completed["status"] == "completed"
    assert duplicate["status"] == "already_completed"
    assert duplicate["automation_run_id"] == completed["automation_run_id"]
    assert degraded["status"] == "completed_with_warnings"
    assert run_count == 2
    assert alert_count >= 1
    assert replay["trade_day_count"] == 2
    assert replay["execution_count"] == 2
    assert all(item["status"] == "dry_run" for item in replay["results"])

    runs_response = await client.get("/api/v1/daily-review/automation-runs")
    alerts_response = await client.get("/api/v1/daily-review/alerts")
    dry_replay_response = await client.post(
        "/api/v1/daily-review/replay",
        json={
            "start_date": "2026-08-28",
            "end_date": "2026-08-28",
            "phases": ["postmarket"],
            "persist": False,
        },
    )
    assert runs_response.status_code == 200
    assert runs_response.json()["count"] == 2
    assert alerts_response.status_code == 200
    assert alerts_response.json()["count"] >= 1
    assert dry_replay_response.status_code == 200
    assert dry_replay_response.json()["persisted"] is False


@pytest.mark.asyncio
async def test_review_default_snapshot_context_tracks_phase(review_env):
    SessionLocal, _client = review_env
    async with SessionLocal() as session:
        await _seed_review_data(session)
        expected = {
            "premarket": "promotion_2000",
            "intraday": "promotion_0935",
            "postmarket": "promotion_2000",
        }
        for phase, snapshot_context in expected.items():
            result = await build_daily_review_snapshot(
                session,
                review_date=date(2026, 8, 28),
                phase=phase,
                snapshot_context="",
                persist=False,
            )
            assert result["snapshot_context"] == snapshot_context


@pytest.mark.asyncio
async def test_intraday_review_excludes_spot_rows_after_as_of(review_env):
    SessionLocal, _client = review_env
    today = date.today()
    as_of_at = datetime.combine(today, datetime.min.time()).replace(hour=10)
    async with SessionLocal() as session:
        await _seed_review_data(session)
        session.add_all(
            [
                StockSpot(
                    code="009901",
                    name="截止前快照",
                    price=10.3,
                    change_pct=3.0,
                    updated_at=as_of_at - timedelta(minutes=1),
                ),
                StockSpot(
                    code="009902",
                    name="截止后快照",
                    price=9.1,
                    change_pct=-9.0,
                    updated_at=as_of_at + timedelta(minutes=1),
                ),
            ]
        )
        await session.flush()
        dimensions, presence = await _market_dimensions(
            session,
            analysis_trade_date=date(2026, 8, 28),
            review_date=today,
            phase="intraday",
            as_of_at=as_of_at,
            plan_snapshots=[],
        )

    assert presence["intraday_spot"] is False  # 只有1/21样本，不认证为完整市场宽度
    assert dimensions["technical"]["source"] == "intraday_spot"
    assert dimensions["technical"]["universe_count"] == 1
    assert dimensions["technical"]["median_return"] == 3.0


@pytest.mark.asyncio
async def test_high_board_learning_tracks_promotion_rate_and_premium(review_env):
    SessionLocal, _client = review_env
    today = date(2026, 8, 28)
    yesterday = date(2026, 8, 27)
    async with SessionLocal() as session:
        for code, chg in (("000001", 5.0), ("000002", 2.0)):
            for trade_day in (yesterday, today):
                session.add(
                    StockKline(
                        code=code,
                        trade_date=trade_day,
                        open=10,
                        high=11,
                        low=9,
                        close=10.2,
                        prev_close=10,
                        change_pct=chg,
                        volume=1000,
                    )
                )
        # 昨日两只高标（连板），今日只有 000001 继续晋级，000002 断板。
        session.add(LimitUpPool(code="000001", name="甲", trade_date=yesterday, consecutive_days=2, source="test"))
        session.add(LimitUpPool(code="000002", name="乙", trade_date=yesterday, consecutive_days=3, source="test"))
        session.add(LimitUpPool(code="000001", name="甲", trade_date=today, consecutive_days=3, source="test"))
        await session.commit()

        dimensions, _presence = await _market_dimensions(
            session,
            analysis_trade_date=today,
            review_date=today,
            phase="postmarket",
            as_of_at=datetime.combine(today, datetime.min.time()).replace(hour=20),
            plan_snapshots=[],
        )

    learning = dimensions["limit_up_learning"]
    assert learning["high_board_promotion_rate"] == pytest.approx(0.5)
    assert learning["high_board_break_rate"] == pytest.approx(0.5)
    assert learning["high_board_continue_count"] == 1
    assert learning["high_board_previous_count"] == 2
    assert learning["high_board_premium_pct"] == pytest.approx(3.5)
    assert learning["previous_board_premium_pct"] == pytest.approx(3.5)
    assert learning["previous_trade_date"] == yesterday.isoformat()


@pytest.mark.asyncio
async def test_prediction_review_counts_only_exact_non_quarantined_second_boards(review_env):
    SessionLocal, _client = review_env
    outcome_day = date(2026, 8, 28)
    async with SessionLocal() as session:
        await _seed_review_data(session)
        session.add_all(
            [
                LimitUpPool(
                    code="000011",
                    name="三板不属于晋二板",
                    trade_date=outcome_day,
                    consecutive_days=3,
                    source="test",
                ),
                LimitUpPool(
                    code="000012",
                    name="隔离二板",
                    trade_date=outcome_day,
                    consecutive_days=2,
                    source="test",
                    quarantined=True,
                ),
                LimitUpPool(
                    code="000013",
                    name="风控屏蔽二板",
                    trade_date=outcome_day,
                    consecutive_days=2,
                    source="test",
                ),
                LimitUpPool(
                    code="300001",
                    name="创业板观察二板",
                    trade_date=outcome_day,
                    consecutive_days=2,
                    source="test",
                ),
                StockTag(
                    code="000013",
                    name="风控屏蔽二板",
                    board_type="main_sz",
                    board_tag="blocked",
                    is_st=True,
                ),
            ]
        )
        await session.commit()
        result = await build_daily_review_snapshot(
            session,
            review_date=outcome_day,
            phase="postmarket",
            persist=False,
        )

    target = result["prediction_review"]["targets"]["2"]
    assert target["snapshot_complete"] is True
    assert target["actual_count"] == 1
    assert target["ranked_hit_count"] == 1
    assert target["actual_universe_scope"] == "tradeable_main_board_non_quarantined"


@pytest.mark.asyncio
async def test_news_statistics_cover_full_as_of_window_not_only_selected_items(review_env):
    SessionLocal, _client = review_env
    target = date(2026, 8, 28)
    as_of_at = datetime.combine(target, time(hour=20, minute=0))
    async with SessionLocal() as session:
        await _seed_review_data(session)
        for index in range(145):
            session.add(
                FinanceNews(
                    source="test",
                    source_id=f"window-news-{index}",
                    title=f"窗口新闻 {index}",
                    publish_time=as_of_at - timedelta(minutes=index + 1),
                    importance=1,
                    sentiment="neutral",
                    bull_bear="neutral",
                    nlp_status="raw",
                )
            )
        await session.commit()
        dimensions, _presence = await _market_dimensions(
            session,
            analysis_trade_date=target,
            review_date=target,
            phase="postmarket",
            as_of_at=as_of_at,
            plan_snapshots=[],
        )

    news = dimensions["news"]
    assert news["count"] == 146
    assert news["selected_count"] == 100
    assert news["selection_limit"] == 100
    assert news["selection_basis"] == "importance_then_publish_time"
    assert len(news["items"]) == 100
    assert news["items"][0]["nlp_status"] == "analyzed"
    assert news["items"][0]["impact_scope"] == "stock_and_sector"
    assert news["items"][0]["has_specific_target"] is True
    assert news["positive_count"] == 1
    assert news["neutral_count"] == 0
    assert news["unanalyzed_count"] == 145
    assert (
        news["positive_count"]
        + news["negative_count"]
        + news["neutral_count"]
        + news["unanalyzed_count"]
        == news["count"]
    )
    fundamental = dimensions["fundamental"]
    assert fundamental["status"] == "unavailable"
    assert fundamental["availability_reason"] == "prediction_plan_missing"
    assert fundamental["candidate_count"] == 0
    assert "没有基本面统计对象" in fundamental["scope"]


@pytest.mark.asyncio
async def test_historical_fundamental_reads_forward_snapshot_if_available(review_env):
    SessionLocal, _client = review_env
    today = date(2026, 8, 28)
    plan_codes = {f"001{index:03d}" for index in range(3)}
    async with SessionLocal() as session:
        for code in plan_codes:
            session.add(StockKline(code=code, trade_date=today, open=10, high=11, low=9, close=10.2, prev_close=10, change_pct=1, volume=1000))
        # 只给其中 2 只需历史基本面快照；第 3 只缺失 → 保持 unavailable 语义但不算入样本。
        for code in sorted(plan_codes)[:2]:
            session.add(StockFundamentalDaily(code=code, name="历史", trade_date=today, pe_ttm=20 + int(code[-1]), pb=3.0, net_profit_growth=15.0, circ_market_cap=50.0, source="test"))
        await session.commit()

        plan_snapshots = []
        for code in sorted(plan_codes):
            plan_snapshots.append(type("PS", (), {"code": code})())
        dimensions, _presence = await _market_dimensions(
            session,
            analysis_trade_date=today,
            review_date=today,
            phase="postmarket",
            as_of_at=datetime.combine(today, datetime.min.time()).replace(hour=20),
            plan_snapshots=plan_snapshots,
        )

    fundamental = dimensions["fundamental"]
    assert fundamental["status"] == "daily_snapshot"
    assert fundamental["availability_reason"] is None
    assert "不单独产生买点" in fundamental["purpose"]
    assert fundamental["candidate_count"] == 2
    assert fundamental["requested_candidate_count"] == 3
    assert fundamental["coverage_ratio"] == pytest.approx(2 / 3, abs=1e-6)
    assert fundamental["valid_pe_count"] == 2
    assert fundamental["valid_pb_count"] == 2
    assert fundamental["valid_growth_count"] == 2
    assert fundamental["median_pe_ttm"] == pytest.approx(20.5)
    assert fundamental["median_pb"] == pytest.approx(3.0)
    assert fundamental["median_net_profit_growth"] == pytest.approx(15.0)
    assert all(item["circ_market_cap"] == 50.0 for item in fundamental["candidates"])


@pytest.mark.asyncio
async def test_gpt_report_disabled_fails_closed(review_env, monkeypatch):
    SessionLocal, client = review_env
    monkeypatch.setattr("app.review.gpt_report.settings.REVIEW_GPT_ENABLED", False)
    async with SessionLocal() as session:
        with pytest.raises(GptReportDisabledError):
            await generate_gpt_report(
                session,
                review_date=date(2026, 8, 28),
                phase="postmarket",
                snapshot_id=1,
            )

    capability = await client.get("/api/v1/daily-review/gpt-reports/capabilities")
    assert capability.status_code == 200
    assert capability.json()["enabled"] is False
    assert capability.json()["mode"] == "frozen_snapshot_only"


@pytest.mark.asyncio
async def test_gpt_report_generates_idempotently(review_env, monkeypatch, tmp_path):
    SessionLocal, client = review_env
    fake_script = tmp_path / "fake_gpt.py"
    fake_script.write_text(
        "import sys, json\n"
        "ctx=json.load(sys.stdin)\n"
        "print(json.dumps({'summary':'测试报告','snapshot_id':ctx['snapshot_identity']['id'],'snapshot_version':ctx['snapshot_identity']['data_version'],'sections':[{'title':ctx['phase'],'body':'ok'}]}))\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("app.review.gpt_report.settings.REVIEW_GPT_ENABLED", True)
    monkeypatch.setattr("app.review.gpt_report.settings.REVIEW_GPT_CLI", f"python3 {fake_script}")
    async with SessionLocal() as session:
        await _seed_review_data(session)
        snapshot = await build_daily_review_snapshot(
            session,
            review_date=date(2026, 8, 28),
            phase="postmarket",
            persist=True,
        )
        await session.commit()
        first = await generate_gpt_report(
            session,
            review_date=date(2026, 8, 28),
            phase="postmarket",
            snapshot_id=snapshot["id"],
        )
        second = await generate_gpt_report(
            session,
            review_date=date(2026, 8, 28),
            phase="postmarket",
            snapshot_id=snapshot["id"],
        )

    matching_reports = await client.get(
        "/api/v1/daily-review/gpt-reports",
        params={"snapshot_id": snapshot["id"]},
    )
    other_reports = await client.get(
        "/api/v1/daily-review/gpt-reports",
        params={"snapshot_id": snapshot["id"] + 999},
    )
    missing_binding = await client.post(
        "/api/v1/daily-review/gpt-reports/generate",
        json={"review_date": "2026-08-28", "phase": "postmarket"},
    )

    assert first["status"] == "generated"
    assert first["summary"] == "测试报告"
    assert first["review_date"] == "2026-08-28"
    assert first["snapshot_id"] == snapshot["id"]
    assert first["content"]["snapshot_id"] == snapshot["id"]
    assert first["content"]["snapshot_version"] == snapshot["data_version"]
    assert second["id"] == first["id"]
    assert second["content"]["sections"][0]["title"] == "postmarket"
    assert matching_reports.status_code == 200
    assert matching_reports.json()["count"] == 1
    assert other_reports.json()["count"] == 0
    assert missing_binding.status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("phase", "as_of_at", "message"),
    [
        ("premarket", datetime(2026, 8, 28, 10, 0), "早于 09:30"),
        ("intraday", datetime(2026, 8, 28, 20, 0), "09:30-15:00"),
        ("postmarket", datetime(2026, 8, 28, 14, 0), "不早于 15:00"),
    ],
)
async def test_review_phase_rejects_out_of_window_as_of(
    review_env,
    phase,
    as_of_at,
    message,
):
    SessionLocal, _client = review_env
    async with SessionLocal() as session:
        await _seed_review_data(session)
        with pytest.raises(ValueError, match=message):
            await build_daily_review_snapshot(
                session,
                review_date=date(2026, 8, 28),
                phase=phase,
                as_of_at=as_of_at,
                persist=False,
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["premarket", "intraday", "postmarket"])
async def test_review_rejects_future_target_day(review_env, phase):
    SessionLocal, _client = review_env
    future_date = date.today() + timedelta(days=1)
    async with SessionLocal() as session:
        with pytest.raises(ValueError, match="不能晚于当前日期"):
            await build_daily_review_snapshot(
                session,
                review_date=future_date,
                phase=phase,
                persist=False,
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["premarket", "intraday"])
async def test_review_rejects_non_trade_target_day(review_env, phase):
    SessionLocal, _client = review_env
    async with SessionLocal() as session:
        await _seed_review_data(session)
        with pytest.raises(ValueError, match="不是交易日"):
            await build_daily_review_snapshot(
                session,
                review_date=date(2026, 8, 29),
                phase=phase,
                persist=False,
            )


@pytest.mark.asyncio
async def test_missing_market_sources_remain_null_and_degrade_decision_view(review_env):
    SessionLocal, _client = review_env
    target = date(2026, 8, 28)
    async with SessionLocal() as session:
        session.add(
            StockKline(
                code="000001",
                trade_date=target,
                open=10,
                high=10,
                low=10,
                close=10,
                prev_close=10,
                change_pct=None,
                volume=100,
            )
        )
        await session.flush()
        dimensions, presence = await _market_dimensions(
            session,
            analysis_trade_date=target,
            review_date=target,
            phase="postmarket",
            as_of_at=datetime.combine(target, time(hour=20, minute=30)),
            plan_snapshots=[],
        )

    assert presence["stock_kline"] is False
    assert dimensions["technical"]["change_coverage"] == 0.0
    assert dimensions["capital"]["market_main_net_inflow"] is None
    assert dimensions["capital"]["sampled_stock_main_net_inflow"] is None
    assert dimensions["technical"]["advance_ratio"] is None
    assert dimensions["technical"]["limit_up_count"] is None
    assert dimensions["technical"]["seal_rate"] is None
    assert dimensions["limit_up_learning"]["count"] is None
    conclusion, guide = build_review_views(
        {
            "phase": "postmarket",
            "analysis_trade_date": target.isoformat(),
            "as_of_at": datetime.combine(target, time(hour=20, minute=30)).isoformat(),
            "dimensions": dimensions,
            "source_presence": presence,
            "quality": {"status": "insufficient", "score": 0.0, "warnings": ["数据源 stock_kline 缺失"]},
            "market_regime": {"primary_regime": "unknown", "primary_regime_name": "未知"},
            "prediction_plan": {"targets": {}},
            "prediction_review": {"status": "snapshot_incomplete"},
        },
        guidance_trade_date=date(2026, 8, 31),
    )
    assert conclusion["stance_label"] == "市场证据不足"
    assert conclusion["confidence"] == 0.0
    assert "市场宽度不足" in conclusion["headline"]
    assert "涨停 数据不足" in conclusion["evidence"][1]["value"]
    assert guide["stance_label"] == "市场证据不足"


@pytest.mark.asyncio
async def test_degraded_sentiment_is_exposed_but_does_not_pass_source_quality(review_env):
    SessionLocal, _client = review_env
    target = date(2026, 8, 28)
    as_of_at = datetime.combine(target, time(hour=20, minute=30))
    async with SessionLocal() as session:
        session.add(
            StockKline(
                code="000001",
                trade_date=target,
                open=10,
                high=10.5,
                low=9.8,
                close=10.2,
                prev_close=10,
                change_pct=2,
                volume=100,
            )
        )
        session.add(
            MarketSentiment(
                trade_date=target,
                sentiment_cycle="divergence",
                limit_up_count=0,
                limit_down_count=0,
                broken_limit_count=0,
                seal_rate=0,
                board_height=0,
                turnover_total=1.0,
                main_net_inflow=0,
                quality_status="degraded",
                quality_reason="测试降级",
                observed_at=as_of_at - timedelta(minutes=1),
            )
        )
        await session.commit()
        dimensions, presence = await _market_dimensions(
            session,
            analysis_trade_date=target,
            review_date=target,
            phase="postmarket",
            as_of_at=as_of_at,
            plan_snapshots=[],
        )

    assert presence["market_sentiment"] is False
    assert dimensions["technical"]["sentiment_quality_status"] == "degraded"
    assert dimensions["technical"]["sentiment_quality_reason"] == "测试降级"


@pytest.mark.asyncio
async def test_sentiment_observed_after_as_of_is_not_read_into_snapshot(review_env):
    SessionLocal, _client = review_env
    target = date(2026, 8, 28)
    as_of_at = datetime.combine(target, time(hour=15, minute=10))
    async with SessionLocal() as session:
        session.add(
            StockKline(
                code="000001",
                trade_date=target,
                open=10,
                high=10.5,
                low=9.8,
                close=10.2,
                prev_close=10,
                change_pct=2,
                volume=100,
            )
        )
        session.add(
            MarketSentiment(
                trade_date=target,
                sentiment_cycle="recovery",
                limit_up_count=10,
                limit_down_count=2,
                broken_limit_count=3,
                seal_rate=76.9,
                board_height=2,
                turnover_total=1.0,
                main_net_inflow=20,
                quality_status="ok",
                observed_at=as_of_at + timedelta(hours=1),
            )
        )
        await session.commit()
        dimensions, presence = await _market_dimensions(
            session,
            analysis_trade_date=target,
            review_date=target,
            phase="postmarket",
            as_of_at=as_of_at,
            plan_snapshots=[],
        )

    assert presence["market_sentiment"] is False
    assert dimensions["technical"]["sentiment_quality_status"] is None
    assert dimensions["technical"]["turnover_total_trillion"] is None


@pytest.mark.asyncio
async def test_postmarket_review_rejects_date_without_same_day_close(review_env):
    SessionLocal, client = review_env
    async with SessionLocal() as session:
        await _seed_review_data(session)
        with pytest.raises(ValueError, match="尚无完整收盘数据"):
            await build_daily_review_snapshot(
                session,
                review_date=date(2026, 8, 31),
                phase="postmarket",
                persist=False,
            )

    response = await client.post(
        "/api/v1/daily-review/snapshots/build",
        json={"review_date": "2026-08-31", "phase": "postmarket", "persist": True},
    )
    assert response.status_code == 409
    assert "最近可用交易日为 2026-08-28" in response.json()["detail"]


@pytest.mark.asyncio
async def test_same_day_postmarket_review_waits_for_close_data(review_env, monkeypatch):
    SessionLocal, _client = review_env

    class BeforeCloseDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls.combine(date(2026, 8, 28), time(hour=14, minute=30))

    monkeypatch.setattr("app.review.service.datetime", BeforeCloseDateTime)
    async with SessionLocal() as session:
        await _seed_review_data(session)
        with pytest.raises(ValueError, match="等待 15:05"):
            await build_daily_review_snapshot(
                session,
                review_date=date(2026, 8, 28),
                phase="postmarket",
                persist=False,
            )


@pytest.mark.asyncio
async def test_gpt_report_requires_a_frozen_snapshot(review_env, monkeypatch):
    SessionLocal, _client = review_env
    monkeypatch.setattr("app.review.gpt_report.settings.REVIEW_GPT_ENABLED", True)
    monkeypatch.setattr("app.review.gpt_report.settings.REVIEW_GPT_CLI", "python3 -c pass")
    async with SessionLocal() as session:
        with pytest.raises(ValueError, match="冻结复盘快照不存在"):
            await generate_gpt_report(
                session,
                review_date=date(2026, 8, 28),
                phase="postmarket",
                snapshot_id=999999,
            )
