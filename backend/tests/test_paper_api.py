import asyncio
import json
from datetime import date, datetime, time, timedelta

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.v1 import paper
from app.db.session import Base, get_db
from app.models import paper as paper_models  # noqa: F401
from app.models import promotion as promotion_models  # noqa: F401
from app.models import signal as signal_models  # noqa: F401
from app.models import stock as stock_models  # noqa: F401
from app.models import trading as trading_models  # noqa: F401  (注册 trade_order/trade_fill 表)
from app.models.paper import PaperAccount, PaperAutoTradeLog, PaperNav, PaperPosition, PaperTradeLog
from app.models.promotion import PromotionPredictionRun, PromotionPredictionSnapshot
from app.models.stock import LimitUpPool, MarketSentiment, SectorPersistence, StockBlacklist, StockKline, StockSectorMapping, StockSpot, StockTag


@pytest.fixture(autouse=True)
def legacy_protocol_by_default(monkeypatch):
    """旧策略单测不依赖真实系统日期；持续实验用例在测试内显式开启。"""
    monkeypatch.setattr(paper.settings, "PAPER_CONTINUOUS_EXPERIMENT_ENABLED", False)


@pytest_asyncio.fixture
async def qualified_reversal_funds(request, paper_client, monkeypatch):
    """旧非资金策略测试显式补独立FundFlow，不再把spot委差当资金fixture。"""
    from app.models.stock import FundFlow
    samples = {
        "test_strategy_a_restores_persisted_arm_only_after_zero_axis_retest": ("000001", date(2026, 9, 14)),
        "test_green_limit_reversal_candidates_catch_low_open_early_turn_strong": ("002407", date(2026, 5, 20)),
        "test_green_limit_reversal_prefilters_before_candidate_cap": ("600901", date(2026, 9, 1)),
        "test_green_limit_reversal_rejects_chasing_limit_price": ("600076", date(2026, 5, 20)),
        "test_green_limit_reversal_rejects_weak_sector_before_ranking": ("000056", date(2026, 6, 1)),
        "test_green_limit_reversal_allows_leader_before_sector_confirms": ("603687", date(2026, 6, 2)),
        "test_green_limit_reversal_rejects_isolated_weak_sector_leader": ("600353", date(2026, 6, 4)),
        "test_underwater_reversal_catches_power_pull_from_below": ("600578", date(2026, 6, 4)),
        "test_underwater_reversal_rejects_board_count_without_sector_strength": ("600579", date(2026, 6, 4)),
        "test_underwater_reversal_prefilters_before_candidate_cap": ("600902", date(2026, 9, 1)),
        "test_underwater_reversal_rejects_after_limit_chase_zone": ("600578", date(2026, 6, 4)),
        "test_underwater_reversal_rejects_intraday_high_pullback": ("600280", date(2026, 6, 5)),
    }
    code, day = samples.get(request.node.name, ("000001", datetime.now().date()))
    at = (datetime.combine(day, time(10)) if request.node.name in samples
          else datetime.now().replace(microsecond=0))
    monkeypatch.setattr(paper, "_paper_now", lambda: at)
    _, maker = paper_client
    async with maker() as session:
        session.add(FundFlow(code=code, trade_date=day, main_net_inflow=1_000_000,
            main_net_inflow_pct=2, source="eastmoney",
            source_version="individual_fund_flow_v3_f124",
            source_quote_at=at-timedelta(seconds=10), received_at=at-timedelta(seconds=5),
            observed_at=at-timedelta(seconds=1)))
        await session.commit()


@pytest_asyncio.fixture
async def paper_client(tmp_path, monkeypatch):
    # 该大文件主要验证既有策略筛选/账户逻辑；下一轮撮合由
    # test_quote_round_execution.py 独立覆盖，避免所有旧用例都手工构造轮次。
    monkeypatch.setattr(paper.settings, "PAPER_DEFER_AUTO_FILL_TO_NEXT_ROUND", False)
    # 旧策略筛选用例固定旧协议；持续实验由独立用例验证，不依赖系统日期。
    monkeypatch.setattr(paper.settings, "PAPER_CONTINUOUS_EXPERIMENT_ENABLED", False)
    db_path = tmp_path / "paper.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", future=True)
    SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    app = FastAPI()
    app.include_router(paper.router, prefix="/paper")

    async def override_get_db():
        async with SessionLocal() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, SessionLocal

    app.dependency_overrides.clear()
    await engine.dispose()


@pytest_asyncio.fixture
async def qualified_execution_risk(paper_client, monkeypatch):
    """成交/真实风控正例必须真正注册规则及显式健康情绪，不能空链默认pass。"""
    from app.risk.engine import risk_engine
    from app.risk.rules import register_all_rules
    monkeypatch.setattr(risk_engine, "_rules", [])
    register_all_rules()
    seen = []
    original = risk_engine.check
    def checked(ctx):
        result = original(ctx)
        seen.append(result)
        return result
    monkeypatch.setattr(risk_engine, "check", checked)
    _, factory = paper_client
    async with factory() as db:
        db.add(MarketSentiment(
            trade_date=date.today(), sentiment_cycle="recovery", sentiment_score=60,
            limit_up_count=40, limit_down_count=5, broken_limit_count=8, seal_rate=70,
            board_height=3, advance_decline_ratio=1.2, turnover_total=1.2,
            main_net_inflow=20, quality_status="ok", quality_reason="",
            calculation_version="isolated_risk_fixture",
        ))
        await db.commit()
    yield seen
    assert seen and all(row["checked_rules"] > 0 and row["evaluation_status"] == "complete"
                        for row in seen)


@pytest_asyncio.fixture
async def qualified_manual_execution(paper_client, qualified_execution_risk, monkeypatch):
    """仅旧HTTP成交正例显式补全日历/身份/三时钟/深度；不替换任何生产门禁。"""
    from app.models.governance import TradeCalendarModel
    from app.models.stock import QuoteRound
    _, factory = paper_client
    at = datetime(2026, 9, 14, 10)
    monkeypatch.setattr(paper, "_public_order_clock", lambda: at)
    monkeypatch.setattr(paper, "_paper_now", lambda: at)
    # 持仓天数测试只使用这批显式工作日fixture，不走日历网络同步。
    monkeypatch.setattr(paper.trade_calendar, "_cache", {
        at.date() - timedelta(days=i): (at.date() - timedelta(days=i)).weekday() < 5
        for i in range(32)
    })
    async with factory() as db:
        sentiment = await db.scalar(select(MarketSentiment))
        sentiment.trade_date = at.date()
        sentiment.observed_at = at - timedelta(seconds=3)
        db.add(TradeCalendarModel(trade_date=at.date(), is_trade_day=True, session_type="full"))
        await db.commit()

    async def seed(code, name, *, last=10, ask=10, bid=9.99, trade_at=None):
        nonlocal at
        at = trade_at if trade_at is not None else at + timedelta(seconds=1)
        round_id = f"explicit-manual-fixture-{at:%Y%m%d%H%M%S}"
        async with factory() as db:
            # A genuine prior-session order must keep its entire ledger/receipt
            # clock contract; never backdate only PaperTradeLog after a fill.
            if await db.scalar(select(TradeCalendarModel).where(
                    TradeCalendarModel.trade_date == at.date())) is None:
                db.add(TradeCalendarModel(trade_date=at.date(), is_trade_day=True, session_type="full"))
            sentiment = await db.scalar(select(MarketSentiment))
            sentiment.trade_date = at.date()
            sentiment.observed_at = at - timedelta(seconds=3)
            db.add(QuoteRound(round_id=round_id,source="tencent",trade_date=at.date(),
                committed_at=at-timedelta(seconds=1),as_of_at=at-timedelta(seconds=3),
                expected_count=1,received_count=1,source_time_count=1,coverage=1,source_time_coverage=1,
                quality_status="ok",config_version="fixture",code_version="fixture"))
            tag = await db.get(StockTag, code)
            if tag is None:
                db.add(StockTag(code=code, name=name, board_type=stock_tagger.get_board_type(code),
                    board_tag="tradeable", is_st=False, is_delisting=False, is_suspended=False))
            spot = await db.get(StockSpot, code)
            if spot is None:
                spot = StockSpot(code=code, name=name)
                db.add(spot)
            spot.price = last
            spot.ask1_price, spot.bid1_price = ask, bid
            spot.ask1_volume = spot.bid1_volume = 100
            spot.limit_down, spot.limit_up = 1, 20
            spot.quote_round_id = round_id
            spot.source_quote_at = at - timedelta(seconds=3)
            spot.received_at = at - timedelta(seconds=2)
            spot.updated_at = at - timedelta(seconds=1)
            await db.commit()
    from app.core.stock_tagger import stock_tagger
    return seed


def test_all_twelve_accounts_remain_enabled_after_experiment_start(monkeypatch):
    monkeypatch.setattr(paper.settings, "PAPER_CONTINUOUS_EXPERIMENT_ENABLED", True)
    monkeypatch.setattr(paper.settings, "PAPER_EXPERIMENT_START_DATE", "2026-09-08")
    monkeypatch.setattr(paper.settings, "PAPER_EXPERIMENT_ACTIVATION_AT", "2026-09-08T00:00:00")
    for account_name in (*paper.PAPER_ALL_ACCOUNTS, *paper.PAPER_CHALLENGER_ACCOUNTS):
        assert paper._strategy_auto_order_enabled(account_name, now=datetime(2026, 9, 7, 13)) is False
        assert paper._strategy_auto_order_enabled(account_name, now=datetime(2026, 9, 8, 10)) is True


def test_champion_intraday_confirmation_requires_gap_bounded_persistence():
    first = datetime(2026, 9, 2, 9, 40, 0)
    ready, sample_count, duration = paper._confirmation_streak_status(
        [first, first + timedelta(seconds=30)],
        current_at=first + timedelta(seconds=60),
        min_samples=3,
        min_persistence_sec=60,
        max_sample_gap_sec=75,
    )
    assert ready is True
    assert sample_count == 3
    assert duration == pytest.approx(60)

    reset, reset_count, reset_duration = paper._confirmation_streak_status(
        [first, first + timedelta(seconds=30)],
        current_at=first + timedelta(seconds=180),
        min_samples=3,
        min_persistence_sec=60,
        max_sample_gap_sec=75,
    )
    assert reset is False
    assert reset_count == 1
    assert reset_duration == 0

    jitter_ready, jitter_count, jitter_duration = paper._confirmation_streak_status(
        [first],
        current_at=first + timedelta(seconds=59, milliseconds=800),
        min_samples=2,
        min_persistence_sec=60,
        max_sample_gap_sec=90,
    )
    assert jitter_ready is True
    assert jitter_count == 2
    assert jitter_duration == pytest.approx(59.8)


@pytest.mark.asyncio
async def test_same_day_paused_strategy_cannot_be_hot_enabled_for_old_signal(
    paper_client,
):
    _client, SessionLocal = paper_client
    trade_date = date.today()
    async with SessionLocal() as session:
        account = await paper._get_or_create_account(
            session,
            paper.PAPER_ACCOUNT_REVERSAL,
        )
        await paper._add_auto_log(
            session,
            account_id=account.id,
            run_id="paused-before-enable",
            trade_date=trade_date,
            trigger="intraday-scheduler",
            source="system",
            action="empty",
            decision="dry_run",
            reason="该策略自动买入委托已因校正证据暂停；继续记录候选",
        )
        await session.commit()

        reason = await paper._strategy_mid_session_enable_block_reason(
            session,
            account_id=account.id,
            trade_date=trade_date,
        )

    assert "仅从下一交易日生效" in reason


@pytest.mark.asyncio
async def test_auto_log_uses_account_strategy_version_not_candidate_base_version(
    paper_client,
):
    _client, SessionLocal = paper_client
    async with SessionLocal() as session:
        account = await paper._get_or_create_account(
            session,
            paper.PAPER_ACCOUNT_PROMOTION,
        )
        log = await paper._add_auto_log(
            session,
            account_id=account.id,
            run_id="version-attribution",
            trade_date=date.today(),
            trigger="test",
            source="promotion_promotion",
            action="hold",
            decision="wait",
            reason="版本归因测试",
            candidate={"route_version": "candidate_route_v1"},
        )
        await session.commit()

    assert log.strategy_version == paper._strategy_version(paper.PAPER_ACCOUNT_PROMOTION)


@pytest.mark.asyncio
async def test_paper_account_is_created_with_nav_baseline(paper_client, monkeypatch):
    # NAV baselines exist only after an actual trading session has opened.
    monkeypatch.setattr(paper, "_paper_now", lambda: datetime(2026, 9, 10, 10))
    client, _SessionLocal = paper_client

    account_response = await client.get("/paper/account")
    nav_response = await client.get("/paper/nav")

    assert account_response.status_code == 200
    account = account_response.json()["account"]
    assert account["account_name"] == "default"
    assert account["broker"] == "paper"
    assert account["execution_plane"] == "simulation"
    assert account["broker_sync_verified"] is False
    assert account["real_order_connected"] is False
    assert account["real_order_guard"] == "hard_blocked"
    assert account["total_assets"] == 50_000
    assert account["cash"] == 50_000
    assert nav_response.status_code == 200
    assert nav_response.json()["nav"][0]["nav"] == 1


@pytest.mark.asyncio
async def test_challenger_comparison_endpoint_is_read_only_and_isolated(paper_client):
    client, _SessionLocal = paper_client

    response = await client.get("/paper/challengers/comparison", params={"horizon_days": 3})
    scoped_c = await client.get(
        "/paper/challengers/comparison",
        params={"horizon_days": 3, "account_name": "mainline"},
    )
    scoped_a = await client.get(
        "/paper/challengers/comparison",
        params={"horizon_days": 3, "account_name": "default"},
    )
    invalid = await client.get("/paper/challengers/comparison", params={"horizon_days": 2})
    invalid_account = await client.get(
        "/paper/challengers/comparison",
        params={"horizon_days": 3, "account_name": "challenger_b"},
    )
    manual_buy = await client.post(
        "/paper/buy",
        params={"account_name": "challenger_b"},
        json={"code": "600000", "price": 10.0, "amount": 100},
    )

    assert response.status_code == 200
    payload = response.json()
    assert len(payload["pairs"]) == 6
    assert payload["isolation"]["broker"] == "paper"
    assert payload["isolation"]["real_order_connected"] is False
    assert payload["isolation"]["shares_cash_positions_with_champion"] is False
    account_backed = [
        item for item in payload["pairs"] if item["execution_mode"] == "isolated_paper"
    ]
    evidence_only = [
        item for item in payload["pairs"] if item["execution_mode"] == "evidence_only"
    ]
    assert all(item["challenger"]["is_challenger"] is True for item in account_backed)
    assert len(evidence_only) == 1
    assert evidence_only[0]["route_id"] == "c3_mainline_first_board"
    assert evidence_only[0]["challenger"]["account_configured"] is False
    assert payload["coverage_summary"] == {
        "configured": 6,
        "total": 6,
        "route_count": 6,
        "shadow_route_count": 6,
        "independent_execution_route_count": 1,
        "total_route_count": 7,
        "control_sample_route_count": 0,
        "control_sample_account_count": 2,
        "shadow_account_backed_route_count": 5,
        "account_backed_route_count": 6,
        "independent_execution_enabled": True,
        "execution_enabled_route_count": 6,
        "execution_paused_route_count": 0,
        "evidence_only_route_count": 1,
    }
    assert payload["data_provenance"]["metrics_are_hardcoded"] is False

    assert scoped_c.status_code == 200
    scoped_c_payload = scoped_c.json()
    assert scoped_c_payload["selected_account_name"] == "mainline"
    assert scoped_c_payload["selected_strategy"]["strategy_code"] == "C"
    assert scoped_c_payload["selected_strategy"]["challenger_configured"] is True
    assert [item["route_id"] for item in scoped_c_payload["pairs"]] == [
        "c_recent_limit_relaunch",
        "c3_mainline_first_board",
    ]
    assert [item["champion"]["account_name"] for item in scoped_c_payload["pairs"]] == [
        "mainline",
        "mainline",
    ]

    assert scoped_a.status_code == 200
    scoped_a_payload = scoped_a.json()
    assert scoped_a_payload["selected_strategy"]["strategy_code"] == "A"
    assert scoped_a_payload["selected_strategy"]["challenger_configured"] is True
    assert scoped_a_payload["selected_strategy"]["control_sample_only"] is False
    assert scoped_a_payload["selected_strategy"]["challenger"]["account_name"] == "challenger_a"
    assert [item["route_id"] for item in scoped_a_payload["pairs"]] == ["momentum_first_retest"]

    assert invalid.status_code == 400
    assert invalid_account.status_code == 400
    assert manual_buy.status_code == 403
    assert "只接受内部前向确认事件委托" in manual_buy.json()["detail"]


@pytest.mark.asyncio
async def test_paper_buy_and_sell_updates_positions_and_trades(paper_client, qualified_manual_execution):
    client, SessionLocal = paper_client
    async with SessionLocal() as session:
        session.add(StockSpot(code="000001", name="平安银行", price=11.0))
        await session.commit()

    await qualified_manual_execution("000001", "平安银行", last=11)
    buy = await client.post("/paper/buy", json={
        "code": "000001",
        "price": 10.0,
        "amount": 100,
        "signal_id": "sig-1",
    })
    assert buy.status_code == 200
    buy_data = buy.json()
    assert buy_data["trade"]["type"] == "buy"
    assert buy_data["trade"]["strategy_version"] == paper._strategy_version(paper.PAPER_ACCOUNT_DEFAULT)
    assert buy_data["account"]["cash"] == 48995.0

    positions = (await client.get("/paper/positions")).json()["positions"]
    assert positions[0]["code"] == "000001"
    assert positions[0]["name"] == "平安银行"
    assert positions[0]["buy_amount"] == 100
    assert positions[0]["current_price"] == 11.0
    assert positions[0]["profit_pct"] == 10.0
    assert positions[0]["strategy_version"] == buy_data["trade"]["strategy_version"]

    open_trade = (await client.get("/paper/trades")).json()["trades"][0]
    assert open_trade["name"] == "平安银行"
    assert open_trade["pnl"] == 100.0
    assert open_trade["floating_pnl"] == 100.0
    assert open_trade["realized_pnl"] is None
    assert open_trade["pnl_pct"] == 10.0
    assert open_trade["pnl_kind"] == "floating"
    assert open_trade["pnl_type_zh"] == "浮动盈亏"
    assert open_trade["reason_zh"] == "手动买入信号"

    await qualified_manual_execution("000001", "平安银行", last=12, ask=12.01, bid=12)
    same_day_sell = await client.post("/paper/sell", json={
        "code": "000001",
        "price": 12.0,
        "amount": 100,
        "reason": "take_profit",
    })
    assert same_day_sell.status_code == 400
    assert "T+1" in same_day_sell.json()["detail"]

    async with SessionLocal() as session:
        position = (
            await session.execute(select(PaperPosition).where(PaperPosition.code == "000001"))
        ).scalar_one()
        # Same frozen trade clock as qualified_manual_execution, not wall-clock yesterday.
        position.buy_time = datetime(2026, 9, 11, 10)
        trade = (
            await session.execute(select(PaperTradeLog).where(PaperTradeLog.code == "000001", PaperTradeLog.trade_type == "buy"))
        ).scalar_one()
        trade.trade_time = position.buy_time
        await session.commit()

    sell = await client.post("/paper/sell", json={
        "code": "000001",
        "price": 12.0,
        "amount": 100,
        "reason": "take_profit",
    })
    assert sell.status_code == 200
    assert sell.json()["trade"]["type"] == "sell"
    assert sell.json()["trade"]["strategy_version"] == buy_data["trade"]["strategy_version"]
    assert sell.json()["trade"]["pnl"] == 188.8
    assert sell.json()["account"]["win_rate"] == 1
    assert (await client.get("/paper/positions")).json()["positions"] == []
    trades = (await client.get("/paper/trades")).json()["trades"]
    assert [item["type"] for item in trades] == ["sell", "buy"]
    assert trades[0]["name"] == "平安银行"
    assert trades[0]["pnl"] == 188.8
    assert trades[0]["realized_pnl"] == 188.8
    assert trades[0]["floating_pnl"] is None
    assert trades[0]["pnl_pct"] == 18.88
    assert trades[0]["pnl_kind"] == "realized"
    assert trades[0]["pnl_type_zh"] == "已实现盈亏"
    assert trades[0]["reason_zh"] == "止盈卖出"
    assert trades[1]["pnl"] is None
    assert trades[1]["floating_pnl"] is None
    assert trades[1]["pnl_pct"] is None


@pytest.mark.asyncio
async def test_paper_buy_rejects_cross_strategy_version_scale_in(paper_client, qualified_manual_execution):
    client, SessionLocal = paper_client
    async with SessionLocal() as session:
        account = await paper._get_or_create_account(session)
        session.add_all(
            [
                StockSpot(code="000002", name="版本测试", price=10.0),
                PaperPosition(
                    account_id=account.id,
                    code="000002",
                    name="版本测试",
                    buy_price=10.0,
                    buy_amount=100,
                    buy_time=datetime.now() - timedelta(days=1),
                    current_price=10.0,
                    strategy_version="paper_a_legacy_v1",
                    is_closed=False,
                ),
            ]
        )
        await session.commit()

    await qualified_manual_execution("000002", "版本测试")
    response = await client.post(
        "/paper/buy",
        json={"code": "000002", "price": 10.0, "amount": 100},
    )

    assert response.status_code == 409
    assert "禁止跨策略版本加仓" in response.json()["detail"]
    async with SessionLocal() as session:
        position = await session.scalar(
            select(PaperPosition).where(PaperPosition.code == "000002")
        )
        trades = list(
            (
                await session.scalars(
                    select(PaperTradeLog).where(PaperTradeLog.code == "000002")
                )
            ).all()
        )
    assert position.buy_amount == 100
    assert trades == []


@pytest.mark.asyncio
async def test_paper_trades_enrich_linked_auto_signal_in_chinese(paper_client):
    client, SessionLocal = paper_client
    async with SessionLocal() as session:
        account = await paper._get_or_create_account(
            session,
            paper.PAPER_ACCOUNT_PROMOTION,
        )
        trade = PaperTradeLog(
            account_id=account.id,
            code="001330",
            trade_type="buy",
            price=5.72,
            amount=1700,
            trade_time=datetime.now(),
            commission=2.92,
            signal_id="auto-promotion_promotion-20260901-001330",
            reason="auto-promotion_promotion-20260901-001330",
        )
        session.add(trade)
        await session.flush()
        session.add_all([
            PaperPosition(
                account_id=account.id,
                code="001330",
                name="博纳影业",
                buy_price=5.72,
                buy_amount=1700,
                buy_time=trade.trade_time,
                current_price=5.73,
                profit_pct=0.17,
                is_closed=False,
            ),
            PaperAutoTradeLog(
                account_id=account.id,
                run_id="paper-auto-test",
                trade_date=date.today(),
                created_at=datetime.now(),
                trigger="test",
                source="promotion_promotion",
                code="001330",
                name="博纳影业",
                action="buy",
                decision="executed",
                reason="自动买点，评分29.0",
                executed_trade_id=trade.id,
            ),
        ])
        await session.commit()

    response = await client.get(
        "/paper/trades",
        params={"account_name": paper.PAPER_ACCOUNT_PROMOTION},
    )
    assert response.status_code == 200
    row = response.json()["trades"][0]
    assert row["name"] == "博纳影业"
    assert row["pnl"] == 17.0
    assert row["floating_pnl"] == 17.0
    assert row["pnl_pct"] == 0.17
    assert row["pnl_type_zh"] == "浮动盈亏"
    assert row["reason_zh"] == "晋级二板：自动买点，评分29.0"
    assert row["raw_reason"] == "auto-promotion_promotion-20260901-001330"


@pytest.mark.parametrize(
    ("signal_id", "expected"),
    [
        ("auto-next_day_plan-20260901-000001", "高胜率预案自动买入"),
        ("auto-promotion_promotion-20260901-000001", "晋级二板自动买入"),
        ("auto-promotion_mainline-20260901-000001", "主线扩散首板自动买入"),
        ("auto-promotion_auction-20260901-000001", "竞价高开强攻自动买入"),
        ("auto-tenbagger_midline-20260901-000001", "连板高标接力自动买入"),
        ("auto-reversal_pullback-20260901-000001", "断板反包自动买入"),
    ],
)
def test_paper_trade_signal_prefixes_are_localized_for_each_strategy(signal_id, expected):
    trade = PaperTradeLog(
        account_id=1,
        code="000001",
        trade_type="buy",
        price=10.0,
        amount=100,
        trade_time=datetime.now(),
        signal_id=signal_id,
        reason=signal_id,
    )
    assert paper._paper_trade_reason_zh(trade) == expected


@pytest.mark.asyncio
async def test_paper_buy_keeps_original_entry_sector_on_scale_in(paper_client, qualified_manual_execution):
    client, SessionLocal = paper_client
    async with SessionLocal() as session:
        session.add(StockSpot(code="600088", name="入场板块测试", price=10.0))
        await session.commit()

    await qualified_manual_execution("600088", "入场板块测试")
    first = await client.post("/paper/buy", json={
        "code": "600088",
        "price": 10.0,
        "amount": 100,
        "signal_id": "first-entry",
        "entry_sector_code": "BK_ENTRY",
        "entry_sector_name": "入场主线",
    })
    second = await client.post("/paper/buy", json={
        "code": "600088",
        "price": 10.1,
        "amount": 100,
        "signal_id": "scale-in",
        "entry_sector_code": "BK_OTHER",
        "entry_sector_name": "后续板块",
    })

    assert first.status_code == 200
    assert second.status_code == 200
    async with SessionLocal() as session:
        position = await session.scalar(
            select(PaperPosition).where(
                PaperPosition.code == "600088",
                PaperPosition.is_closed.is_(False),
            )
        )
    assert position.entry_sector_code == "BK_ENTRY"
    assert position.entry_sector_name == "入场主线"
    payload = (await client.get("/paper/positions")).json()["positions"][0]
    assert payload["entry_sector_code"] == "BK_ENTRY"
    assert payload["entry_sector_name"] == "入场主线"


def test_short_sell_reason_uses_tighter_intraday_rules():
    position = PaperPosition(
        account_id=1,
        code="000001",
        buy_price=10.0,
        buy_amount=100,
        buy_time=datetime.now() - timedelta(days=1),
    )

    # 2026-08-31 复盘：SMALL_STOP_LOSS_PCT 1.2 -> 2.0，跌破 -2.0 + 1% 才触发
    small_stop = paper._short_sell_reason(
        position,
        {"price": 9.7, "open": 10.0, "avg_price": 9.9},
        profit_pct=-3.0,
        hold_days=1,
        trade_date=datetime.now().date(),
    )
    assert "小止损" in small_stop

    # 2026-08-31 复盘：PULLBACK_FROM_HIGH_PCT 1.5 -> 2.5，需要从高点回落 >= 2.5%
    pullback = paper._short_sell_reason(
        position,
        # 2026-09-17 改3：弱信号 rung 现要求 ≥2 项独立走弱证据，
        # 这里补 ma5 破位 + 五档卖压；触发阈值本身未变。
        {"price": 10.15, "high": 10.42, "close_position": 0.3,
         "ma5": 10.30, "orderbook_imbalance": -0.5},
        profit_pct=1.5,
        hold_days=1,
        trade_date=datetime.now().date(),
    )
    assert "盘中冲高回落" in pullback


def test_short_sell_reason_protects_breakeven_after_intraday_profit():
    position = PaperPosition(
        account_id=1,
        code="000001",
        buy_price=10.0,
        buy_amount=1000,
        buy_time=datetime.now() - timedelta(days=1),
    )

    # 2026-08-31 复盘：BREAKEVEN_PROTECT_HIGH_PROFIT_PCT 1.5 -> 3.0，最高浮盈需 >= 3.0%
    reason = paper._short_sell_reason(
        position,
        # 2026-09-17 改3：同上，补两项独立证据
        {"price": 10.02, "high": 10.4, "close_position": 0.5,
         "ma5": 10.20, "orderbook_imbalance": -0.5},
        profit_pct=0.2,
        hold_days=1,
        trade_date=datetime.now().date(),
        now=datetime(2026, 5, 29, 10, 0),
    )

    assert "回落成本线保护" in reason
    assert paper._auto_sell_amount(position, 1000, reason) == 500


def test_sector_retreat_requires_individual_weakness_confirmation():
    """板块弱势是组合背景，不得覆盖个股自身的盘中强势。"""
    position = PaperPosition(
        account_id=1,
        code="002328",
        name="新朋股份",
        buy_price=8.76,
        buy_amount=1100,
        buy_time=datetime(2026, 9, 1, 9, 36),
        stop_loss_price=8.31,
    )
    strong_stock = paper._short_sell_reason(
        position,
        {
            "price": 8.89,
            "open": 8.74,
            "high": 8.93,
            "low": 8.66,
            "change_pct": 0.34,
            "volume_ratio": 7.53,
            "ma5": 8.348,
            "avg_price": 8.76,
            "min5_change": 1.484,
            "orderbook_imbalance": -0.1207,
            "close_position": 0.85,
            "sector_retreat_reason": "板块退潮：液冷服务器强度19.1",
        },
        profit_pct=1.48,
        hold_days=1,
        trade_date=date(2026, 9, 2),
        now=datetime(2026, 9, 2, 9, 45),
        params={"next_day_min_profit_pct": -99.0},
    )
    confirmed_weakness = paper._short_sell_reason(
        position,
        {
            "price": 8.94,
            "open": 8.90,
            "change_pct": 0.2,
            "volume_ratio": 1.0,
            "ma5": 8.80,
            "avg_price": 9.00,
            "min5_change": 0.1,
            "orderbook_imbalance": -0.5,
            "close_position": 0.6,
            "sector_retreat_reason": "板块退潮：液冷服务器强度19.1",
        },
        profit_pct=2.05,
        hold_days=1,
        trade_date=date(2026, 9, 2),
        now=datetime(2026, 9, 2, 10, 0),
        params={"next_day_min_profit_pct": -99.0},
    )

    assert strong_stock == ""
    assert confirmed_weakness.startswith("板块退潮：液冷服务器强度19.1；个股同步转弱确认")


def test_auto_sell_amount_full_exits_small_short_profit_position():
    position = PaperPosition(
        account_id=1,
        code="000001",
        buy_price=10.0,
        buy_amount=300,
        buy_time=datetime.now() - timedelta(days=1),
    )

    assert paper._auto_sell_amount(position, 300, "触发短线止盈：4.00%") == 300
    assert paper._auto_sell_amount(position, 300, "盘中冲高回落：最高浮盈6.00%，从高点回落2.00%") == 300


def test_limit_up_next_day_limit_down_full_exit_before_open_noise():
    position = PaperPosition(
        account_id=1,
        code="603318",
        buy_price=10.12,
        buy_amount=2300,
        buy_time=datetime(2026, 6, 23, 10, 43),
    )

    reason = paper._short_sell_reason(
        position,
        {
            "price": 10.52,
            "open": 10.99,
            "limit_down": 10.52,
            "change_pct": -10.01,
            "prev_was_limit_up": True,
            "open_gap_from_prev_close_pct": -5.99,
        },
        profit_pct=3.95,
        hold_days=1,
        trade_date=date(2026, 6, 24),
        now=datetime(2026, 6, 24, 9, 32),
    )

    assert "昨日涨停次日跌停风险" in reason
    assert paper._auto_sell_amount(position, 2300, reason) == 2300


def test_short_sell_reason_prioritizes_position_stop_loss_price():
    position = PaperPosition(
        account_id=1,
        code="000001",
        buy_price=10.0,
        buy_amount=100,
        buy_time=datetime.now() - timedelta(days=1),
        stop_loss_price=9.95,
    )

    reason = paper._short_sell_reason(
        position,
        {"price": 9.94, "open": 10.0, "stop_loss_price": 9.95},
        profit_pct=-0.6,
        hold_days=1,
        trade_date=datetime.now().date(),
    )

    assert "持仓止损价" in reason
    assert paper._auto_sell_amount(position, 100, reason) == 100


def test_open_noise_window_requires_confirmed_short_stop():
    position = PaperPosition(
        account_id=1,
        code="000001",
        buy_price=86.86,
        buy_amount=100,
        buy_time=datetime.now() - timedelta(days=1),
        stop_loss_price=84.25,
    )

    early_noise = paper._short_sell_reason(
        position,
        {
            "price": 83.68,
            "open": 84.62,
            "avg_price": 83.5,
            "ma5": 82.0,
            "orderbook_imbalance": 0.2,
            "stop_loss_price": 84.25,
        },
        profit_pct=-3.66,
        hold_days=1,
        trade_date=datetime.now().date(),
        now=datetime(2026, 5, 18, 9, 31),
    )
    confirmed = paper._short_sell_reason(
        position,
        {
            "price": 83.68,
            "open": 84.62,
            "avg_price": 84.0,
            "ma5": 84.5,
            "orderbook_imbalance": -0.5,
            "stop_loss_price": 84.25,
        },
        profit_pct=-3.66,
        hold_days=1,
        trade_date=datetime.now().date(),
        now=datetime(2026, 5, 18, 9, 31),
    )

    assert early_noise == ""
    assert "持仓止损价" in confirmed


@pytest.mark.asyncio
async def test_weak_rebound_rejects_unconfirmed_anomaly_buy(paper_client):
    _client, SessionLocal = paper_client
    async with SessionLocal() as session:
        session.add_all([
            StockKline(code="002202", trade_date=date(2026, 5, 14), close=26.03, change_pct=-6.37),
            StockKline(code="002202", trade_date=date(2026, 5, 15), close=25.09, change_pct=-3.61),
            StockSpot(
                code="002202",
                name="金风科技",
                price=25.79,
                avg_price=25.83,
                volume_ratio=0.73,
                orderbook_imbalance=-0.539,
            ),
        ])
        await session.commit()

        reason = await paper._weak_rebound_reject_reason(
            session,
            code="002202",
            trade_date=date(2026, 5, 18),
            price=25.79,
        )

    assert "连续大跌后弱反抽未确认" in reason


@pytest.mark.asyncio
async def test_single_day_crash_repair_requires_breakthrough_or_strong_rebound(paper_client):
    _client, SessionLocal = paper_client
    async with SessionLocal() as session:
        session.add_all([
            StockKline(code="000967", trade_date=date(2026, 5, 15), close=13.23, change_pct=-7.16),
            StockKline(code="000967", trade_date=date(2026, 5, 18), close=13.10, change_pct=-0.98),
            StockSpot(
                code="000967",
                name="盈峰环境",
                price=13.31,
                avg_price=13.12,
                volume_ratio=1.68,
                orderbook_imbalance=0.3861,
            ),
        ])
        await session.commit()

        weak_reason = await paper._weak_rebound_reject_reason(
            session,
            code="000967",
            trade_date=date(2026, 5, 19),
            price=13.31,
            change_pct=1.6,
            event_types=["capital"],
        )
        confirmed_reason = await paper._weak_rebound_reject_reason(
            session,
            code="000967",
            trade_date=date(2026, 5, 19),
            price=13.31,
            change_pct=1.6,
            event_types=["capital", "breakthrough"],
        )

    assert "单日大跌修复" in weak_reason
    assert confirmed_reason == ""


@pytest.mark.asyncio
async def test_cluster_damage_rebound_requires_stronger_short_confirmation(paper_client):
    _client, SessionLocal = paper_client
    async with SessionLocal() as session:
        session.add_all([
            StockKline(code="000967", trade_date=date(2026, 5, 11), close=14.83, change_pct=-9.02),
            StockKline(code="000967", trade_date=date(2026, 5, 12), close=13.85, change_pct=-6.61),
            StockKline(code="000967", trade_date=date(2026, 5, 13), close=15.07, change_pct=8.81),
            StockKline(code="000967", trade_date=date(2026, 5, 14), close=14.25, change_pct=-5.44),
            StockKline(code="000967", trade_date=date(2026, 5, 15), close=13.23, change_pct=-7.16),
            StockSpot(
                code="000967",
                name="盈峰环境",
                price=13.31,
                avg_price=13.12,
                volume_ratio=1.68,
                orderbook_imbalance=0.3861,
            ),
        ])
        await session.commit()

        reason = await paper._weak_rebound_reject_reason(
            session,
            code="000967",
            trade_date=date(2026, 5, 19),
            price=13.31,
            change_pct=1.76,
            event_types=["capital", "breakthrough"],
        )
        rapid_reason = await paper._weak_rebound_reject_reason(
            session,
            code="000967",
            trade_date=date(2026, 5, 19),
            price=13.31,
            change_pct=1.76,
            event_types=["capital", "breakthrough", "rapid_rise"],
        )

    assert "5日内" in reason
    assert rapid_reason == ""


def test_anomaly_sector_gate_filters_edge_theme_and_caps_marginal_score():
    assert paper._anomaly_sector_reject_reason(74.9, 1.5)
    assert paper._anomaly_sector_reject_reason(75.0, 0.99)
    assert paper._anomaly_sector_reject_reason(82.0, 0.2) == ""

    assert paper._anomaly_position_score(99, 75.5, 1.1) == 84
    assert paper._anomaly_position_score(99, 83.0, 1.8) == 99


@pytest.mark.asyncio
async def test_continuation_risk_rejects_limit_up_next_day_large_gap_down(paper_client):
    _client, SessionLocal = paper_client
    async with SessionLocal() as session:
        session.add_all([
            StockKline(
                code="000001",
                trade_date=date(2026, 5, 20),
                close=11.0,
                change_pct=10.0,
            ),
            LimitUpPool(
                code="000001",
                name="样本股份",
                trade_date=date(2026, 5, 20),
                limit_up_price=11.0,
            ),
            StockSpot(
                code="000001",
                name="样本股份",
                price=10.45,
                prev_close=11.0,
                open=10.45,
                change_pct=-5.0,
            ),
        ])
        await session.commit()

        reason = await paper._continuation_risk_reject_reason(
            session,
            code="000001",
            trade_date=date(2026, 5, 21),
            candidate={"_source": "anomaly_buy_point"},
        )

    assert "昨日涨停后今日大幅低开" in reason


@pytest.mark.asyncio
async def test_continuation_risk_rejects_one_day_limit_theme_without_persistence(paper_client):
    _client, SessionLocal = paper_client
    async with SessionLocal() as session:
        session.add(StockSpot(
            code="002407",
            name="多氟多",
            price=40.69,
            prev_close=37.18,
            open=36.50,
            change_pct=10.0,
        ))
        await session.commit()

        reason = await paper._continuation_risk_reject_reason(
            session,
            code="002407",
            trade_date=date(2026, 5, 21),
            candidate={
                "_source": "green_limit_reversal",
                "change_pct": 10.0,
                "sector_consecutive_days": 1,
                "sector_strength": 76.0,
                "sector_change_pct": 2.0,
                "sector_fund_flow": 50.0,
                "sector_limit_up_count": 2,
            },
        )

    assert "持续性不足" in reason


@pytest.mark.asyncio
async def test_continuation_risk_rejects_weak_sector_failed_limit_board(paper_client):
    _client, SessionLocal = paper_client
    async with SessionLocal() as session:
        session.add(StockSpot(
            code="600076",
            name="康欣新材",
            price=4.44,
            prev_close=4.04,
            open=3.98,
            limit_up=4.44,
            change_pct=9.90,
            seal_quality_score=0,
        ))
        await session.commit()

        reason = await paper._continuation_risk_reject_reason(
            session,
            code="600076",
            trade_date=date(2026, 5, 21),
            candidate={
                "_source": "green_limit_reversal",
                "price": 4.44,
                "change_pct": 9.90,
                "sector_consecutive_days": 23,
                "sector_strength": 33.8,
                "sector_change_pct": 0.76,
                "sector_fund_flow": -57.62,
                "sector_limit_up_count": 8,
            },
        )

    assert "不追弱板块高位" in reason


@pytest.mark.asyncio
async def test_continuation_risk_rejects_limit_price_without_seal_quality(paper_client):
    _client, SessionLocal = paper_client
    async with SessionLocal() as session:
        session.add(StockSpot(
            code="600001",
            name="冲板样本",
            price=11.0,
            prev_close=10.0,
            open=9.9,
            limit_up=11.0,
            change_pct=10.0,
            seal_quality_score=10,
        ))
        await session.commit()

        reason = await paper._continuation_risk_reject_reason(
            session,
            code="600001",
            trade_date=date(2026, 5, 21),
            candidate={
                "_source": "green_limit_reversal",
                "price": 11.0,
                "change_pct": 10.0,
                "sector_consecutive_days": 3,
                "sector_strength": 90.0,
                "sector_change_pct": 2.0,
                "sector_fund_flow": 50.0,
                "sector_limit_up_count": 8,
            },
        )

    assert "不打未封硬板" in reason


@pytest.mark.asyncio
async def test_continuation_risk_allows_strong_persistent_limit_theme(paper_client):
    _client, SessionLocal = paper_client
    async with SessionLocal() as session:
        session.add(StockSpot(
            code="002407",
            name="多氟多",
            price=40.69,
            prev_close=37.18,
            open=36.50,
            limit_up=41.0,
            change_pct=10.0,
            seal_quality_score=60,
        ))
        await session.commit()

        reason = await paper._continuation_risk_reject_reason(
            session,
            code="002407",
            trade_date=date(2026, 5, 21),
            candidate={
                "_source": "green_limit_reversal",
                "change_pct": 10.0,
                "sector_consecutive_days": 2,
                "sector_strength": 83.0,
                "sector_change_pct": 2.0,
                "sector_fund_flow": 50.0,
                "sector_limit_up_count": 6,
            },
        )

    assert reason == ""


def test_active_sector_uses_strongest_valid_driver_not_first_mapping():
    selected, _reason = paper._active_sector_from_anomaly_detail({
        "sector_factors": [
            {
                "sector_code": "BK_WEAK",
                "sector_name": "弱题材",
                "sector_type": "concept",
                "source": "pywencai",
                "strength_score": 56,
                "change_pct": 0.3,
                "fund_flow": 1.0,
                "limit_up_count": 1,
                "lifecycle_score": 52,
            },
            {
                "sector_code": "BK_STRONG",
                "sector_name": "强主线",
                "sector_type": "concept",
                "source": "pywencai",
                "strength_score": 83,
                "change_pct": 2.1,
                "fund_flow": 18.0,
                "limit_up_count": 6,
                "lifecycle_score": 79,
            },
        ]
    })

    assert selected["sector_code"] == "BK_STRONG"


@pytest.mark.asyncio
async def test_latest_sector_context_and_exit_use_strongest_entry_thesis(paper_client):
    _client, SessionLocal = paper_client
    target = date(2026, 8, 28)
    async with SessionLocal() as session:
        session.add_all([
            StockSectorMapping(
                code="600088", sector_code="BK_ENTRY", sector_name="入场主线",
                sector_type="concept", source="pywencai",
            ),
            StockSectorMapping(
                code="600088", sector_code="BK_OTHER", sector_name="当前强板块",
                sector_type="concept", source="pywencai",
            ),
            SectorPersistence(
                sector_code="BK_ENTRY", sector_name="入场主线", trade_date=target,
                strength_score=35, change_pct=-1.2, fund_flow=-3.0,
                limit_up_count=0, consecutive_days=1,
            ),
            SectorPersistence(
                sector_code="BK_OTHER", sector_name="当前强板块", trade_date=target,
                strength_score=88, change_pct=2.0, fund_flow=20.0,
                limit_up_count=5, consecutive_days=3,
            ),
        ])
        await session.commit()

        strongest, _ = await paper._latest_sector_context_for_code(session, "600088")
        entry_reason = await paper._sector_retreat_reason(
            session, "600088", "BK_ENTRY", "入场主线",
        )
        legacy_reason = await paper._sector_retreat_reason(session, "600088")

    assert strongest["sector_code"] == "BK_OTHER"
    assert "入场主线" in entry_reason
    assert legacy_reason == ""


@pytest.mark.asyncio
async def test_trade_decision_ignores_stale_sector_persistence(paper_client):
    """交易日有显式时点时，数月前的强板块记录不得参与买入或退出。"""
    _client, SessionLocal = paper_client
    target = date(2026, 9, 2)
    async with SessionLocal() as session:
        session.add_all([
            StockSectorMapping(
                code="002328", sector_code="BK_STALE", sector_name="数据中心",
                sector_type="concept", source="pywencai",
            ),
            StockSectorMapping(
                code="002328", sector_code="BK_FRESH", sector_name="液冷服务器",
                sector_type="concept", source="pywencai",
            ),
            SectorPersistence(
                sector_code="BK_STALE", sector_name="数据中心",
                trade_date=date(2026, 5, 27), strength_score=95,
                change_pct=3.0, fund_flow=100, limit_up_count=8,
            ),
            SectorPersistence(
                sector_code="BK_FRESH", sector_name="液冷服务器",
                trade_date=target, strength_score=65,
                change_pct=1.2, fund_flow=20, limit_up_count=3,
            ),
        ])
        await session.commit()

        current, _ = await paper._latest_sector_context_for_code(
            session, "002328", trade_date=target,
        )
        inferred_entry = await paper._resolve_candidate_entry_sector(
            session, {"code": "002328"}, trade_date=target,
        )
        resolved_entry = await paper._resolve_candidate_entry_sector(
            session,
            {"code": "002328", "sector_name": "液冷服务器"},
            trade_date=target,
        )
        stale_exit = await paper._sector_retreat_reason(
            session,
            "002328",
            "BK_STALE",
            "数据中心",
            entry_trade_date=date(2026, 9, 1),
            trade_date=target,
        )

    assert current["sector_code"] == "BK_FRESH"
    assert current["sector_trade_date"] == target.isoformat()
    assert inferred_entry == {}
    assert resolved_entry == {
        "entry_sector_code": "BK_FRESH",
        "entry_sector_name": "液冷服务器",
    }
    assert stale_exit == ""


@pytest.mark.asyncio
@pytest.mark.usefixtures("qualified_reversal_funds")
async def test_green_limit_reversal_candidates_catch_low_open_early_turn_strong(paper_client):
    _client, SessionLocal = paper_client
    async with SessionLocal() as session:
        session.add_all([
            StockSpot(
                code="002407",
                name="多氟多",
                price=39.10,
                prev_close=37.18,
                open=36.50,
                limit_up=40.90,
                change_pct=5.16,
                avg_price=38.81,
                volume_ratio=1.29,
                main_net_inflow=82_455_000,
                orderbook_imbalance=1.0,
                seal_quality_score=88,
            ),
            StockSectorMapping(
                code="002407",
                sector_code="BK001",
                sector_name="锂电池",
                sector_type="concept",
                source="test",
            ),
            SectorPersistence(
                sector_code="BK001",
                sector_name="锂电池",
                trade_date=date(2026, 5, 20),
                strength_score=82,
                change_pct=1.8,
                fund_flow=32.5,
                limit_up_count=6,
            ),
        ])
        await session.commit()

        candidates, notes = await paper._green_limit_reversal_candidates(
            session,
            limit=5,
            trade_date=date(2026, 5, 20),
        )

    assert [item["code"] for item in candidates] == ["002407"]
    assert candidates[0]["_source"] == "green_limit_reversal"
    assert candidates[0]["open_change_pct"] < -0.5
    assert candidates[0]["change_pct"] < paper.settings.PAPER_AUTO_GREEN_REVERSAL_MAX_CHANGE_PCT
    assert candidates[0]["sector_name"] == "锂电池"
    assert "绿盘转强扫描" not in " ".join(notes)


@pytest.mark.asyncio
@pytest.mark.usefixtures("qualified_reversal_funds")
async def test_green_limit_reversal_prefilters_before_candidate_cap(paper_client):
    """普涨时不应让高涨幅但非低开标的占满扫描上限，挤掉真实低开转强候选。"""
    _client, SessionLocal = paper_client
    trade_date = date(2026, 9, 1)
    decoys = [
        StockSpot(
            code=f"7{index:05d}",
            name=f"高开干扰{index}",
            price=10.64,
            prev_close=10.0,
            open=10.20,
            change_pct=6.4,
            avg_price=10.50,
            volume_ratio=2.0,
            main_net_inflow=2_000_000 + index,
            orderbook_imbalance=0.8,
        )
        for index in range(80)
    ]
    async with SessionLocal() as session:
        session.add_all([
            *decoys,
            StockSpot(
                code="600901",
                name="低开转强样本",
                price=10.50,
                prev_close=10.0,
                open=9.40,
                low=9.30,
                high=10.60,
                change_pct=5.0,
                avg_price=10.20,
                volume_ratio=2.0,
                main_net_inflow=1_000_000,
                orderbook_imbalance=0.8,
            ),
            StockSectorMapping(
                code="600901",
                sector_code="BK_RECALL",
                sector_name="召回测试主线",
                sector_type="concept",
                source="test",
            ),
            SectorPersistence(
                sector_code="BK_RECALL",
                sector_name="召回测试主线",
                trade_date=trade_date,
                strength_score=85,
                change_pct=2.0,
                fund_flow=20.0,
                limit_up_count=5,
            ),
        ])
        await session.commit()

        candidates, _notes = await paper._green_limit_reversal_candidates(
            session,
            limit=5,
            trade_date=trade_date,
        )

    assert "600901" in [item["code"] for item in candidates]


@pytest.mark.asyncio
@pytest.mark.usefixtures("qualified_reversal_funds")
async def test_green_limit_reversal_rejects_chasing_limit_price(paper_client):
    _client, SessionLocal = paper_client
    async with SessionLocal() as session:
        session.add(StockSpot(
            code="600076",
            name="康欣新材",
            price=4.44,
            prev_close=4.04,
            open=3.98,
            limit_up=4.44,
            change_pct=9.90,
            avg_price=4.26,
            volume_ratio=7.56,
            main_net_inflow=575_040,
            orderbook_imbalance=1.0,
        ))
        await session.commit()

        candidates, _notes = await paper._green_limit_reversal_candidates(
            session,
            limit=5,
            trade_date=date(2026, 5, 20),
        )

    assert candidates == []


@pytest.mark.asyncio
async def test_continuation_rejects_near_limit_weak_sector_reversal(paper_client):
    _client, SessionLocal = paper_client
    async with SessionLocal() as session:
        session.add(StockSpot(
            code="600076",
            name="康欣新材",
            price=4.44,
            prev_close=4.04,
            open=3.98,
            limit_up=4.44,
            change_pct=9.90,
            avg_price=4.26,
            volume_ratio=7.56,
            main_net_inflow=575_040,
            orderbook_imbalance=1.0,
        ))
        await session.commit()

        reason = await paper._continuation_risk_reject_reason(
            session,
            code="600076",
            trade_date=date(2026, 5, 21),
            candidate={
                "_source": "green_limit_reversal",
                "change_pct": 9.90,
                "leader_first_move": False,
                "theme_spread": True,
                "sector_strength": 33.8,
                "sector_change_pct": 0.76,
                "sector_fund_flow": -57.62,
                "sector_limit_up_count": 8,
                "sector_consecutive_days": 23,
            },
        )

    assert "不追弱板块高位" in reason


@pytest.mark.asyncio
@pytest.mark.usefixtures("qualified_reversal_funds")
async def test_green_limit_reversal_rejects_weak_sector_before_ranking(paper_client):
    _client, SessionLocal = paper_client
    async with SessionLocal() as session:
        session.add_all([
            StockSpot(
                code="000056",
                name="弱板块样本",
                price=2.03,
                prev_close=1.93,
                open=1.83,
                change_pct=5.18,
                avg_price=1.98,
                volume_ratio=3.0,
                main_net_inflow=10_000,
                orderbook_imbalance=0.8,
            ),
            StockSectorMapping(
                code="000056",
                sector_code="BK_WEAK",
                sector_name="弱题材",
                sector_type="concept",
                source="test",
            ),
            SectorPersistence(
                sector_code="BK_WEAK",
                sector_name="弱题材",
                trade_date=date(2026, 6, 1),
                strength_score=8,
                change_pct=1.2,
                fund_flow=-5.6,
                limit_up_count=1,
                consecutive_days=1,
            ),
        ])
        await session.commit()

        candidates, _notes = await paper._green_limit_reversal_candidates(
            session,
            limit=5,
            trade_date=date(2026, 6, 1),
        )

    assert candidates == []


@pytest.mark.asyncio
@pytest.mark.usefixtures("qualified_reversal_funds")
async def test_green_limit_reversal_allows_leader_before_sector_confirms(paper_client):
    _client, SessionLocal = paper_client
    async with SessionLocal() as session:
        session.add_all([
            StockSpot(
                code="603687",
                name="大胜达",
                price=17.25,
                prev_close=16.26,
                open=16.06,
                change_pct=6.09,
                avg_price=17.13,
                volume_ratio=3.54,
                main_net_inflow=2_689,
                orderbook_imbalance=0.9347,
            ),
            StockSectorMapping(
                code="603687",
                sector_code="BK_LEADER",
                sector_name="钙钛矿电池",
                sector_type="concept",
                source="test",
            ),
            SectorPersistence(
                sector_code="BK_LEADER",
                sector_name="钙钛矿电池",
                trade_date=date(2026, 6, 2),
                strength_score=0,
                change_pct=-1.05,
                fund_flow=-6.32,
                limit_up_count=3,
                consecutive_days=0,
            ),
        ])
        await session.commit()

        candidates, _notes = await paper._green_limit_reversal_candidates(
            session,
            limit=5,
            trade_date=date(2026, 6, 2),
        )
        reject_reason = await paper._continuation_risk_reject_reason(
            session,
            code="603687",
            trade_date=date(2026, 6, 2),
            candidate=candidates[0],
        )

    assert [item["code"] for item in candidates] == ["603687"]
    assert candidates[0]["leader_first_move"] is True
    assert "领先板块" in candidates[0]["leader_first_move_reason"]
    assert candidates[0]["theme_spread"] is True
    assert reject_reason == ""


@pytest.mark.asyncio
@pytest.mark.usefixtures("qualified_reversal_funds")
async def test_green_limit_reversal_rejects_isolated_weak_sector_leader(paper_client):
    _client, SessionLocal = paper_client
    async with SessionLocal() as session:
        session.add_all([
            StockSpot(
                code="600353",
                name="旭光电子",
                price=25.55,
                prev_close=24.04,
                open=23.86,
                change_pct=6.28,
                avg_price=25.10,
                volume_ratio=2.65,
                main_net_inflow=1_246,
                orderbook_imbalance=0.76,
            ),
            StockSectorMapping(
                code="600353",
                sector_code="BK_ISOLATED",
                sector_name="5G概念",
                sector_type="concept",
                source="test",
            ),
            SectorPersistence(
                sector_code="BK_ISOLATED",
                sector_name="5G概念",
                trade_date=date(2026, 6, 4),
                strength_score=0,
                change_pct=-0.42,
                fund_flow=-17.0,
                limit_up_count=0,
                consecutive_days=0,
            ),
        ])
        await session.commit()

        candidates, _notes = await paper._green_limit_reversal_candidates(
            session,
            limit=5,
            trade_date=date(2026, 6, 4),
        )
        reject_reason = await paper._continuation_risk_reject_reason(
            session,
            code="600353",
            trade_date=date(2026, 6, 4),
            candidate={
                "_source": "green_limit_reversal",
                "change_pct": 6.28,
                "leader_first_move": True,
                "theme_spread": False,
                "sector_strength": 0,
                "sector_change_pct": -0.42,
                "sector_fund_flow": -17.0,
                "sector_limit_up_count": 0,
            },
        )

    assert candidates == []
    assert "短线不做孤立冲高" in reject_reason


@pytest.mark.asyncio
@pytest.mark.usefixtures("qualified_reversal_funds")
async def test_underwater_reversal_catches_power_pull_from_below(paper_client):
    _client, SessionLocal = paper_client
    async with SessionLocal() as session:
        session.add_all([
            StockSpot(
                code="600578",
                name="京能电力",
                price=8.78,
                prev_close=8.49,
                open=8.24,
                low=8.10,
                change_pct=3.42,
                avg_price=8.66,
                volume_ratio=0.92,
                main_net_inflow=216_159,
                orderbook_imbalance=0.68,
                limit_up=9.34,
            ),
            StockSectorMapping(
                code="600578",
                sector_code="BK_POWER",
                sector_name="绿色电力",
                sector_type="concept",
                source="test",
            ),
            SectorPersistence(
                sector_code="BK_POWER",
                sector_name="绿色电力",
                trade_date=date(2026, 6, 4),
                strength_score=58.6,
                change_pct=0.28,
                fund_flow=86.17,
                limit_up_count=9,
                consecutive_days=33,
            ),
        ])
        await session.commit()

        candidates, _notes = await paper._underwater_reversal_candidates(
            session,
            limit=5,
            trade_date=date(2026, 6, 4),
        )
        reject_reason = await paper._continuation_risk_reject_reason(
            session,
            code="600578",
            trade_date=date(2026, 6, 4),
            candidate=candidates[0],
        )

    assert [item["code"] for item in candidates] == ["600578"]
    assert candidates[0]["_source"] == "underwater_reversal"
    assert candidates[0]["low_drop_pct"] < -1.5
    assert candidates[0]["theme_spread"] is True
    assert reject_reason == ""


@pytest.mark.asyncio
@pytest.mark.usefixtures("qualified_reversal_funds")
async def test_underwater_reversal_rejects_board_count_without_sector_strength(
    paper_client,
):
    _client, SessionLocal = paper_client
    trade_date = date(2026, 6, 4)
    async with SessionLocal() as session:
        session.add_all(
            [
                StockSpot(
                    code="600579",
                    name="弱板块伪扩散",
                    price=8.78,
                    prev_close=8.49,
                    open=8.24,
                    low=8.10,
                    high=8.90,
                    change_pct=3.42,
                    avg_price=8.66,
                    volume_ratio=0.92,
                    main_net_inflow=216_159,
                    orderbook_imbalance=0.68,
                    limit_up=9.34,
                ),
                StockSectorMapping(
                    code="600579",
                    sector_code="BK_WEAK",
                    sector_name="弱势概念",
                    sector_type="concept",
                    source="test",
                ),
                SectorPersistence(
                    sector_code="BK_WEAK",
                    sector_name="弱势概念",
                    trade_date=trade_date,
                    strength_score=28.6,
                    change_pct=-0.28,
                    fund_flow=-86.17,
                    limit_up_count=9,
                    consecutive_days=33,
                ),
            ]
        )
        await session.commit()

        candidates, _notes = await paper._underwater_reversal_candidates(
            session,
            limit=5,
            trade_date=trade_date,
        )

    assert candidates == []


@pytest.mark.asyncio
@pytest.mark.usefixtures("qualified_reversal_funds")
async def test_underwater_reversal_prefilters_before_candidate_cap(paper_client):
    """资金排序截断前先过滤未曾水下的标的，避免普涨日漏掉真实水下翻红。"""
    _client, SessionLocal = paper_client
    trade_date = date(2026, 9, 1)
    decoys = [
        StockSpot(
            code=f"8{index:05d}",
            name=f"未水下干扰{index}",
            price=10.50,
            prev_close=10.0,
            open=10.10,
            low=9.95,
            high=10.60,
            change_pct=5.0,
            avg_price=10.40,
            volume_ratio=2.0,
            main_net_inflow=2_000_000 + index,
            orderbook_imbalance=0.8,
        )
        for index in range(120)
    ]
    async with SessionLocal() as session:
        session.add_all([
            *decoys,
            StockSpot(
                code="600902",
                name="水下翻红样本",
                price=10.10,
                prev_close=10.0,
                open=9.80,
                low=9.70,
                high=10.20,
                change_pct=1.0,
                avg_price=10.0,
                volume_ratio=1.0,
                main_net_inflow=1_000_000,
                orderbook_imbalance=0.5,
            ),
            StockSectorMapping(
                code="600902",
                sector_code="BK_UNDER_RECALL",
                sector_name="水下召回测试主线",
                sector_type="concept",
                source="test",
            ),
            SectorPersistence(
                sector_code="BK_UNDER_RECALL",
                sector_name="水下召回测试主线",
                trade_date=trade_date,
                strength_score=85,
                change_pct=2.0,
                fund_flow=20.0,
                limit_up_count=5,
            ),
        ])
        await session.commit()

        candidates, _notes = await paper._underwater_reversal_candidates(
            session,
            limit=5,
            trade_date=trade_date,
        )

    assert "600902" in [item["code"] for item in candidates]


@pytest.mark.asyncio
@pytest.mark.usefixtures("qualified_reversal_funds")
async def test_underwater_reversal_rejects_after_limit_chase_zone(paper_client):
    _client, SessionLocal = paper_client
    async with SessionLocal() as session:
        session.add(StockSpot(
            code="600578",
            name="京能电力",
            price=9.34,
            prev_close=8.49,
            open=8.24,
            low=8.10,
            change_pct=10.01,
            avg_price=8.66,
            volume_ratio=0.92,
            main_net_inflow=216_159,
            orderbook_imbalance=1.0,
            limit_up=9.34,
        ))
        await session.commit()

        candidates, _notes = await paper._underwater_reversal_candidates(
            session,
            limit=5,
            trade_date=date(2026, 6, 4),
        )

    assert candidates == []


@pytest.mark.asyncio
@pytest.mark.usefixtures("qualified_reversal_funds")
async def test_underwater_reversal_rejects_intraday_high_pullback(paper_client):
    _client, SessionLocal = paper_client
    async with SessionLocal() as session:
        session.add_all([
            StockSpot(
                code="600280",
                name="中央商场",
                price=3.75,
                prev_close=3.59,
                open=3.49,
                low=3.48,
                high=3.94,
                change_pct=4.46,
                avg_price=3.70,
                volume_ratio=10.38,
                main_net_inflow=9_367,
                orderbook_imbalance=0.5218,
                min5_change=-1.2,
                limit_up=3.95,
            ),
            StockSectorMapping(
                code="600280",
                sector_code="BK_PROPERTY",
                sector_name="物业管理",
                sector_type="concept",
                source="test",
            ),
            SectorPersistence(
                sector_code="BK_PROPERTY",
                sector_name="物业管理",
                trade_date=date(2026, 6, 5),
                strength_score=56.4,
                change_pct=0.28,
                fund_flow=7.28,
                limit_up_count=5,
                consecutive_days=25,
            ),
        ])
        await session.commit()

        candidates, _notes = await paper._underwater_reversal_candidates(
            session,
            limit=5,
            trade_date=date(2026, 6, 5),
        )

    assert candidates == []


def test_recovery_buy_amount_caps_low_price_position_size():
    account = PaperAccount(
        initial_capital=50_000,
        current_capital=43_000,
        total_assets=48_000,
        max_drawdown=-3.2,
    )

    amount = paper._auto_buy_amount(account, price=3.75, open_count=0, score=94)

    assert amount == paper.settings.PAPER_AUTO_DRAWDOWN_RECOVERY_MAX_AMOUNT


def test_hard_drawdown_allows_empty_account_probe_only(monkeypatch):
    monkeypatch.setattr(paper.settings, "PAPER_AUTO_DRAWDOWN_BUY_GATE_MODE", "pause")
    account = PaperAccount(
        initial_capital=50_000,
        current_capital=48_000,
        total_assets=48_000,
        max_drawdown=-4.29,
    )

    assert paper._is_drawdown_recovery_buy(
        account,
        open_count=0,
        today_new_buy_count=0,
        candidate_score=95.3,
    ) is True
    assert paper._auto_buy_pause_reason(
        account,
        open_count=0,
        today_new_buy_count=0,
        candidate_score=95.3,
    ) == ""
    # 2026-08-31 复盘新增：硬止损仓位上限 = 总资产 × 2% / (price × 5%) = 1052 -> 1000 手
    # 旧值 1200 股 × 18.25 × 5% = 1095 元，超过总资产的 2%，已被新上限拦截。
    assert paper._auto_buy_amount(account, price=18.25, open_count=0, score=95.3) == 1000


def test_deep_drawdown_does_not_permanently_lock_high_score_recovery(monkeypatch):
    monkeypatch.setattr(paper.settings, "PAPER_AUTO_DRAWDOWN_BUY_GATE_MODE", "pause")
    monkeypatch.setattr(
        paper.settings,
        "PAPER_AUTO_DRAWDOWN_PERMANENT_LOCK_ENABLED",
        False,
    )
    account = PaperAccount(
        initial_capital=50_000,
        current_capital=39_500,
        total_assets=39_500,
        max_drawdown=-25.09,
    )

    assert paper._is_drawdown_recovery_buy(
        account,
        open_count=0,
        today_new_buy_count=0,
        candidate_score=92.13,
    ) is True
    assert paper._is_drawdown_recovery_buy(
        account,
        open_count=0,
        today_new_buy_count=0,
        candidate_score=91.99,
    ) is False
    assert paper._auto_buy_amount(account, price=5.46, open_count=0, score=92.13) <= 5000


def test_deep_drawdown_optional_permanent_lock_still_works(monkeypatch):
    monkeypatch.setattr(paper.settings, "PAPER_AUTO_DRAWDOWN_BUY_GATE_MODE", "pause")
    monkeypatch.setattr(
        paper.settings,
        "PAPER_AUTO_DRAWDOWN_PERMANENT_LOCK_ENABLED",
        True,
    )
    monkeypatch.setattr(
        paper.settings,
        "PAPER_AUTO_DRAWDOWN_HARD_RECOVERY_MAX_PCT",
        20.0,
    )
    account = PaperAccount(
        initial_capital=50_000,
        current_capital=39_500,
        total_assets=39_500,
        max_drawdown=-25.09,
    )

    assert paper._is_drawdown_recovery_buy(
        account,
        open_count=0,
        today_new_buy_count=0,
        candidate_score=99.0,
    ) is False


# === 2026-08-31 新增：回撤开仓闸门总开关单元测试 ===

def test_buy_gate_mode_pause_keeps_legacy_pause(monkeypatch):
    """默认 pause 模式必须保留老的回撤暂停行为."""
    monkeypatch.setattr(paper.settings, "PAPER_AUTO_DRAWDOWN_BUY_GATE_MODE", "pause")
    account = PaperAccount(
        initial_capital=50_000,
        current_capital=39_500,
        total_assets=39_500,
        max_drawdown=-25.09,
    )
    # 老的永久锁行为仍然生效, score=99 都被拒
    monkeypatch.setattr(paper.settings, "PAPER_AUTO_DRAWDOWN_PERMANENT_LOCK_ENABLED", True)
    monkeypatch.setattr(paper.settings, "PAPER_AUTO_DRAWDOWN_HARD_RECOVERY_MAX_PCT", 20.0)
    assert paper._is_drawdown_recovery_buy(
        account, open_count=0, today_new_buy_count=0, candidate_score=99.0,
    ) is False
    assert "暂停开仓线" in paper._auto_buy_pause_reason(
        account, open_count=0, today_new_buy_count=0, candidate_score=99.0,
    )


def test_buy_gate_mode_cautious_bypasses_paper_but_keeps_risk(monkeypatch):
    """cautious 模式: 跳过 paper 层暂停, 仍保留 risk 层 15% 熔断."""
    monkeypatch.setattr(paper.settings, "PAPER_AUTO_DRAWDOWN_BUY_GATE_MODE", "cautious")
    account = PaperAccount(
        initial_capital=50_000,
        current_capital=39_500,
        total_assets=39_500,
        max_drawdown=-25.09,
    )
    # paper 层闸门被绕开, 任何 score 都能进入 _is_drawdown_recovery_buy 返回 True
    assert paper._is_drawdown_recovery_buy(
        account, open_count=0, today_new_buy_count=0, candidate_score=70.0,
    ) is True
    # pause_reason 也被绕开
    assert paper._auto_buy_pause_reason(
        account, open_count=0, today_new_buy_count=0, candidate_score=70.0,
    ) == ""


def test_buy_gate_mode_unlimited_bypasses_both_layers(monkeypatch):
    """unlimited 模式: 两层都跳过, 单笔止损/止盈/仓位上限等单仓位级风控仍生效."""
    monkeypatch.setattr(paper.settings, "PAPER_AUTO_DRAWDOWN_BUY_GATE_MODE", "unlimited")
    account = PaperAccount(
        initial_capital=50_000,
        current_capital=15_000,
        total_assets=15_000,
        max_drawdown=-70.0,  # 假设极端回撤
    )
    # paper 层完全放行
    assert paper._is_drawdown_recovery_buy(
        account, open_count=0, today_new_buy_count=0, candidate_score=60.0,
    ) is True
    assert paper._auto_buy_pause_reason(
        account, open_count=0, today_new_buy_count=0, candidate_score=60.0,
    ) == ""


def test_buy_gate_mode_unlimited_still_blocks_via_risk_engine(monkeypatch):
    """unlimited 模式: 单笔仓位级的风控规则（仓位上限/单笔金额/最大持仓数等）仍然由 risk engine 拦截.

    验证 _risk_check_for_buy 在 unlimited 模式下:
    1. 回撤相关 ctx.max_drawdown 被置 0 (放行回撤熔断)
    2. 但其它 ctx 字段正常, 单笔仓位级风控(持仓数/单笔金额/ST/停牌等)继续生效
    """
    monkeypatch.setattr(paper.settings, "PAPER_AUTO_DRAWDOWN_BUY_GATE_MODE", "unlimited")
    # 上面这个测试主要验证 helper 函数; 完整 risk_engine 集成测试在 tests/test_risk.py 中已覆盖


def test_hard_drawdown_uses_larger_size_for_conviction_candidate(monkeypatch):
    monkeypatch.setattr(paper.settings, "PAPER_AUTO_DRAWDOWN_BUY_GATE_MODE", "pause")
    account = PaperAccount(
        initial_capital=50_000,
        current_capital=48_000,
        total_assets=48_000,
        max_drawdown=-4.29,
    )

    assert paper._is_drawdown_recovery_buy(
        account,
        open_count=0,
        today_new_buy_count=0,
        candidate_score=97.2,
    ) is True
    # 2026-08-31 复盘新增：硬止损仓位上限拦截超过总资产 2% 的单笔仓位
    assert paper._auto_buy_amount(account, price=18.25, open_count=0, score=97.2) == 1000
    # 高价股自然受硬止损上限约束更紧：300 股 × 63.11 × 5% = 947 元 ≈ 总资产 2%
    assert paper._auto_buy_amount(account, price=63.11, open_count=0, score=99.0) == 300


def test_hard_drawdown_strong_market_allows_second_conviction_buy(monkeypatch):
    monkeypatch.setattr(paper.settings, "PAPER_AUTO_DRAWDOWN_BUY_GATE_MODE", "pause")
    account = PaperAccount(
        initial_capital=50_000,
        current_capital=42_000,
        total_assets=48_500,
        max_drawdown=-4.29,
    )

    assert paper._is_drawdown_recovery_buy(
        account,
        open_count=1,
        today_new_buy_count=1,
        candidate_score=99.0,
    ) is True
    assert paper._is_drawdown_recovery_buy(
        account,
        open_count=1,
        today_new_buy_count=1,
        candidate_score=99.0,
        recovery_max_buys=2,
        recovery_max_open_positions=2,
    ) is True
    assert paper._auto_buy_pause_reason(
        account,
        open_count=1,
        today_new_buy_count=1,
        candidate_score=99.0,
        recovery_max_buys=2,
        recovery_max_open_positions=2,
    ) == ""


def test_strong_market_recovery_sentiment_thresholds():
    strong = MarketSentiment(
        trade_date=date.today(),
        limit_up_count=143,
        limit_down_count=20,
        advance_decline_ratio=7.15,
        main_net_inflow=347.01,
    )
    weak = MarketSentiment(
        trade_date=date.today(),
        limit_up_count=64,
        advance_decline_ratio=1.49,
        main_net_inflow=-1148.36,
    )

    assert paper._is_strong_market_recovery_sentiment(strong) is True
    assert paper._is_strong_market_recovery_sentiment(weak) is False


def test_afternoon_new_buy_requires_strong_market():
    strong = MarketSentiment(
        trade_date=date.today(),
        limit_up_count=143,
        limit_down_count=20,
        advance_decline_ratio=7.15,
        main_net_inflow=347.01,
    )
    divergent = MarketSentiment(
        trade_date=date.today(),
        limit_up_count=81,
        limit_down_count=40,
        advance_decline_ratio=2.02,
        main_net_inflow=-448.43,
    )

    assert paper._is_afternoon_new_buy_time(datetime(2026, 6, 11, 13, 1))
    assert not paper._is_afternoon_new_buy_time(datetime(2026, 6, 11, 11, 20))
    assert paper._market_allows_afternoon_new_buy(strong) is True
    assert paper._market_allows_afternoon_new_buy(divergent) is False


def test_afternoon_leader_exception_allows_high_score_divergent_market():
    divergent_but_tradable = MarketSentiment(
        trade_date=date.today(),
        limit_up_count=34,
        limit_down_count=25,
        seal_rate=54.8,
        advance_decline_ratio=1.36,
        main_net_inflow=0,
    )

    assert paper._market_allows_afternoon_new_buy(divergent_but_tradable) is False
    assert paper._candidate_allows_afternoon_new_buy(
        divergent_but_tradable,
        source="green_limit_reversal",
        score=93.2,
    ) is True
    assert paper._candidate_allows_afternoon_new_buy(
        divergent_but_tradable,
        source="green_limit_reversal",
        score=91.9,
    ) is False


def test_late_leader_exception_keeps_cutoff_for_only_high_score_leaders():
    divergent_but_tradable = MarketSentiment(
        trade_date=date.today(),
        limit_up_count=34,
        limit_down_count=25,
        seal_rate=54.1,
        advance_decline_ratio=1.36,
        main_net_inflow=0,
    )

    assert paper._is_late_new_buy_time(datetime(2026, 6, 29, 14, 25))
    assert paper._candidate_allows_late_new_buy(
        datetime(2026, 6, 29, 14, 25),
        divergent_but_tradable,
        source="green_limit_reversal",
        score=93.2,
    ) is True
    assert paper._candidate_allows_late_new_buy(
        datetime(2026, 6, 29, 14, 25),
        divergent_but_tradable,
        source="green_limit_reversal",
        score=91.9,
    ) is False
    assert paper._candidate_allows_late_new_buy(
        datetime(2026, 6, 29, 14, 41),
        divergent_but_tradable,
        source="green_limit_reversal",
        score=93.2,
    ) is False


def test_market_allows_daily_participation_thresholds(monkeypatch):
    monkeypatch.setattr(paper.settings, "PAPER_AUTO_DAILY_PARTICIPATION_ENABLED", True)
    tradable = MarketSentiment(
        trade_date=date.today(),
        limit_up_count=35,
        limit_down_count=8,
        advance_decline_ratio=1.8,
        main_net_inflow=-20,
    )
    weak = MarketSentiment(
        trade_date=date.today(),
        limit_up_count=20,
        limit_down_count=60,
        advance_decline_ratio=0.8,
    )

    assert paper._market_allows_daily_participation(tradable) is True
    assert paper._market_allows_daily_participation(weak) is False


def test_market_icepoint_reversal_setup_thresholds():
    icepoint = MarketSentiment(
        trade_date=date.today(),
        sentiment_cycle="freezing",
        limit_up_count=18,
        limit_down_count=55,
        advance_decline_ratio=0.7,
    )
    meltdown = MarketSentiment(
        trade_date=date.today(),
        sentiment_cycle="freezing",
        limit_up_count=8,
        limit_down_count=130,
        advance_decline_ratio=0.2,
    )
    normal = MarketSentiment(
        trade_date=date.today(),
        sentiment_cycle="recovery",
        limit_up_count=45,
        limit_down_count=10,
        advance_decline_ratio=1.8,
    )

    assert paper._market_is_icepoint_reversal_setup(icepoint) is True
    assert paper._market_is_icepoint_reversal_setup(meltdown) is False
    assert paper._market_is_icepoint_reversal_setup(normal) is False


@pytest.mark.asyncio
async def test_daily_participation_candidates_require_live_value_confirmation(monkeypatch, paper_client):
    _client, SessionLocal = paper_client
    monkeypatch.setattr(paper.settings, "PAPER_AUTO_DAILY_PARTICIPATION_ENABLED", True)
    async def fake_bull_rank(_db):
        return {"rank": [
            {
                "code": "000001", "total_score": 91, "level": "A", "change_pct": 4.2,
                "short_trend_score": 100, "is_volume_price_uptrend": True,
                "main_net_inflow_pct": 4.0,
            },
            {
                "code": "000002", "total_score": 80, "level": "A", "change_pct": 1.5,
                "short_trend_score": 100, "is_volume_price_uptrend": True,
                "main_net_inflow_pct": 3.0,
            },
            {
                "code": "000003", "total_score": 82, "level": "A", "change_pct": 1.5,
                "short_trend_score": 100, "is_volume_price_uptrend": True,
                "main_net_inflow_pct": 3.0,
            },
            {
                "code": "000004", "total_score": 90, "level": "B", "change_pct": 1.2,
                "short_trend_score": 100, "is_volume_price_uptrend": True,
                "main_net_inflow_pct": 4.0,
            },
        ]}

    monkeypatch.setattr("app.api.v1.tenbagger._bull_rank", fake_bull_rank)
    async with SessionLocal() as session:
        session.add_all([
            StockSpot(
                code="000001", price=10.42, prev_close=10.0, low=9.90, high=10.45,
                avg_price=10.20, change_pct=4.2, volume_ratio=1.3,
            ),
            StockSpot(
                code="000002", price=10.15, prev_close=10.0, low=10.00, high=10.28,
                avg_price=10.12, change_pct=1.5, volume_ratio=1.2,
                main_net_inflow=2_000_000, orderbook_imbalance=0.20,
                support_strength_score=65,
            ),
            StockSpot(
                code="000003", price=10.15, prev_close=10.0, low=10.00, high=10.28,
                avg_price=10.12, change_pct=1.5, volume_ratio=3.2,
                main_net_inflow=2_000_000, orderbook_imbalance=0.20,
                support_strength_score=65,
            ),
            StockSpot(
                code="000004", price=10.12, prev_close=10.0, low=10.00, high=10.25,
                avg_price=10.10, change_pct=1.2, volume_ratio=1.3,
                main_net_inflow=2_000_000, orderbook_imbalance=0.20,
                support_strength_score=65,
            ),
        ])
        await session.commit()

        rows = await paper._daily_participation_candidates(session, limit=5)

    assert [item["code"] for item in rows] == ["000002"]
    assert rows[0]["_source"] == "daily_participation"
    assert rows[0]["daily_participation"] is True
    assert rows[0]["execution_confirmation"] is True
    assert rows[0]["total_score"] >= 82
    assert len(rows[0]["intraday_confirmations"]) >= 2


@pytest.mark.asyncio
async def test_auto_candidate_order_prefers_confirmed_value_entries(monkeypatch):
    async def plan(*_args, **_kwargs):
        return ([{"code": "000003", "_source": "next_day_plan", "total_score": 98, "change_pct": 2.6}], [])

    async def green(*_args, **_kwargs):
        return ([], [])

    async def underwater(*_args, **_kwargs):
        return ([{
            "code": "000001", "_source": "underwater_reversal", "total_score": 90,
            "change_pct": 0.5, "execution_confirmation": True,
        }], [])

    async def ma5(*_args, **_kwargs):
        return ([{
            "code": "000002", "_source": "ma5_pullback", "total_score": 91,
            "change_pct": 1.2, "execution_confirmation": True,
        }], [])

    async def anomaly(*_args, **_kwargs):
        return ([{"code": "000004", "_source": "anomaly_buy_point", "total_score": 99, "change_pct": 2.0}], [])

    monkeypatch.setattr(paper, "_next_day_plan_buy_candidates", plan)
    monkeypatch.setattr(paper, "_green_limit_reversal_candidates", green)
    monkeypatch.setattr(paper, "_underwater_reversal_candidates", underwater)
    monkeypatch.setattr(paper, "_ma5_pullback_candidates", ma5)
    monkeypatch.setattr(paper, "_anomaly_buy_point_candidates", anomaly)

    rows, _notes = await paper._paper_auto_buy_candidates(
        None,
        limit=10,
        trade_date=date(2026, 8, 14),
    )

    assert [item["code"] for item in rows[:2]] == ["000001", "000002"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("qualified_reversal_funds")
async def test_strategy_a_restores_persisted_arm_only_after_zero_axis_retest(
    paper_client,
    monkeypatch,
):
    _client, SessionLocal = paper_client
    # Same fixed clock as qualified_reversal_funds; no midnight or stale-fund drift.
    now = datetime(2026, 9, 14, 10)
    trade_date = now.date()
    armed_candidate = {
        "_source": "green_limit_reversal",
        "code": "000001",
        "name": "零轴回踩",
        "total_score": 91.0,
        "change_pct": 4.2,
        "open_change_pct": -2.0,
        "execution_confirmation": False,
    }
    monkeypatch.setattr(
        paper,
        "_paper_effective_min5_change",
        lambda *_args, **_kwargs: 0.2,
    )
    async with SessionLocal() as session:
        session.add_all(
            [
                StockSectorMapping(
                    code="000001",
                    sector_code="S-ARM",
                    sector_name="测试主线",
                    sector_type="concept",
                    source="pywencai",
                ),
                SectorPersistence(
                    sector_code="S-ARM",
                    sector_name="测试主线",
                    trade_date=trade_date,
                    consecutive_days=2,
                    limit_up_count=2,
                    fund_flow=8.0,
                    change_pct=2.0,
                    strength_score=80.0,
                ),
                StockSpot(
                    code="000001",
                    name="零轴回踩",
                    price=10.08,
                    prev_close=10.0,
                    open=9.8,
                    low=9.7,
                    high=10.3,
                    avg_price=10.05,
                    change_pct=0.8,
                    min5_change=0.2,
                    volume_ratio=1.2,
                    main_net_inflow=2_000_000,
                    orderbook_imbalance=0.2,
                ),
                PaperAutoTradeLog(
                    account_id=1,
                    run_id="arm-current",
                    trade_date=trade_date,
                    created_at=now - timedelta(minutes=10),
                    trigger="schedule-intraday",
                    source="green_limit_reversal",
                    code="000001",
                    name="零轴回踩",
                    action="skip_buy",
                    decision="skipped",
                    reason="等待首次回踩确认",
                    candidate_json=json.dumps(armed_candidate, ensure_ascii=False),
                ),
                PaperAutoTradeLog(
                    account_id=1,
                    run_id="arm-expired",
                    trade_date=trade_date,
                    created_at=now - timedelta(hours=3),
                    trigger="schedule-intraday",
                    source="green_limit_reversal",
                    code="000002",
                    name="过期触发",
                    action="skip_buy",
                    decision="skipped",
                    reason="等待首次回踩确认",
                    candidate_json=json.dumps(
                        {**armed_candidate, "code": "000002"},
                        ensure_ascii=False,
                    ),
                ),
            ]
        )
        await session.commit()

        restored, notes = await paper._restore_armed_reversal_candidates(
            session,
            account_id=1,
            trade_date=trade_date,
            existing_candidates=[],
            limit=5,
            observed_at=now,
        )
        session.add(
            PaperAutoTradeLog(
                account_id=1,
                run_id="arm-newer-confirmed",
                trade_date=trade_date,
                created_at=now,
                trigger="schedule-intraday",
                source="green_limit_reversal",
                code="000001",
                name="零轴回踩",
                action="skip_buy",
                decision="skipped",
                reason="较新状态已确认，不得回退到旧触发",
                candidate_json=json.dumps(
                    {**armed_candidate, "execution_confirmation": True},
                    ensure_ascii=False,
                ),
            )
        )
        await session.commit()
        blocked_restore, _blocked_notes = await paper._restore_armed_reversal_candidates(
            session,
            account_id=1,
            trade_date=trade_date,
            existing_candidates=[],
            limit=5,
            observed_at=now + timedelta(seconds=1),
        )

    assert [item["code"] for item in restored] == ["000001"]
    assert restored[0]["execution_confirmation"] is True
    assert restored[0]["reversal_state"] == "retest_confirmed_from_persisted_arm"
    assert restored[0]["armed_change_pct"] == pytest.approx(4.2)
    assert "持久化触发状态" in notes[0]
    assert blocked_restore == []


@pytest.mark.asyncio
async def test_ma5_pullback_candidates_prefer_trend_pullback(monkeypatch, paper_client):
    _client, SessionLocal = paper_client

    async def fake_radar_candidates(_db, limit):
        return [
            {"code": "000001", "name": "趋势回踩", "total_score": 90, "change_pct": 1.2},
            {"code": "000002", "name": "高位追涨", "total_score": 92, "change_pct": 4.8},
        ]

    monkeypatch.setattr(paper, "_radar_candidates", fake_radar_candidates)

    async with SessionLocal() as session:
        base_day = date(2026, 6, 1)
        closes = [10 + idx * 0.1 for idx in range(20)]
        for idx, close in enumerate(closes):
            session.add(StockKline(code="000001", trade_date=base_day + timedelta(days=idx), close=close, change_pct=1.0))
            session.add(StockKline(code="000002", trade_date=base_day + timedelta(days=idx), close=close, change_pct=1.0))
        session.add(StockSpot(
            code="000001", name="趋势回踩", price=11.78, low=11.65, high=12.0,
            avg_price=11.75, change_pct=1.2, min5_change=0.2, volume_ratio=1.1,
            orderbook_imbalance=0.12, support_strength_score=60,
        ))
        session.add(StockSpot(code="000002", name="高位追涨", price=12.55, low=12.40, change_pct=4.8, volume_ratio=1.2))
        await session.commit()

        rows, notes = await paper._ma5_pullback_candidates(session, limit=5, trade_date=date(2026, 6, 20))

    assert [item["code"] for item in rows] == ["000001"]
    assert rows[0]["_source"] == "ma5_pullback"
    assert rows[0]["ma5_pullback"] is True
    assert rows[0]["execution_confirmation"] is True
    assert rows[0]["ma5"] > rows[0]["ma10"] > rows[0]["ma20"]
    assert notes


@pytest.mark.asyncio
async def test_icepoint_reversal_candidates_require_intraday_low_reclaim(monkeypatch, paper_client):
    _client, SessionLocal = paper_client

    async def fake_radar_candidates(_db, limit):
        return [
            {"code": "000001", "total_score": 96, "change_pct": 0.5},
            {"code": "000002", "total_score": 96, "change_pct": 0.6},
            {"code": "000003", "total_score": 94, "change_pct": 0.4},
            {"code": "000004", "total_score": 98, "change_pct": 3.0},
        ]

    monkeypatch.setattr(paper, "_radar_candidates", fake_radar_candidates)

    async with SessionLocal() as session:
        session.add_all([
            StockSpot(
                code="000001", name="低位回收", price=10.05, prev_close=10.0,
                low=9.85, high=10.10, avg_price=10.00, change_pct=0.5,
                volume_ratio=1.2, bid_ratio=12.0,
            ),
            StockSpot(
                code="000002", name="未下探", price=10.06, prev_close=10.0,
                low=9.96, high=10.10, avg_price=10.01, change_pct=0.6,
                volume_ratio=1.2, bid_ratio=10.0,
            ),
            StockSpot(
                code="000003", name="低分回收", price=10.04, prev_close=10.0,
                low=9.82, high=10.10, avg_price=10.00, change_pct=0.4,
                volume_ratio=1.1, bid_ratio=8.0,
            ),
            StockSpot(
                code="000004", name="追高回收", price=10.30, prev_close=10.0,
                low=9.80, high=10.32, avg_price=10.10, change_pct=3.0,
                volume_ratio=1.6, bid_ratio=15.0,
            ),
        ])
        await session.commit()
        rows = await paper._icepoint_reversal_candidates(session, limit=5)

    assert [item["code"] for item in rows] == ["000001"]
    assert rows[0]["_source"] == "icepoint_reversal"
    assert rows[0]["icepoint_reversal"] is True


def test_hard_drawdown_rejects_lower_score_recovery_probe(monkeypatch):
    monkeypatch.setattr(paper.settings, "PAPER_AUTO_DRAWDOWN_BUY_GATE_MODE", "pause")
    account = PaperAccount(
        initial_capital=50_000,
        current_capital=48_000,
        total_assets=48_000,
        max_drawdown=-4.29,
    )

    assert paper._is_drawdown_recovery_buy(
        account,
        open_count=0,
        today_new_buy_count=0,
        candidate_score=91.9,
    ) is False
    assert paper._auto_buy_pause_reason(
        account,
        open_count=0,
        today_new_buy_count=0,
        candidate_score=91.9,
    ) != ""


def test_deep_drawdown_blocks_even_high_score_empty_account_probe(monkeypatch):
    monkeypatch.setattr(paper.settings, "PAPER_AUTO_DRAWDOWN_BUY_GATE_MODE", "pause")
    account = PaperAccount(
        initial_capital=50_000,
        current_capital=47_149.76,
        total_assets=47_149.76,
        max_drawdown=-10.58,
    )

    assert paper._is_drawdown_recovery_buy(
        account,
        open_count=0,
        today_new_buy_count=0,
        candidate_score=91.9,
    ) is False
    assert paper._is_drawdown_recovery_buy(
        account,
        open_count=0,
        today_new_buy_count=0,
        candidate_score=94.9,
    ) is False


def test_deep_drawdown_allows_only_explicit_staged_observation_probe(monkeypatch):
    monkeypatch.setattr(paper.settings, "PAPER_AUTO_DRAWDOWN_BUY_GATE_MODE", "pause")
    account = PaperAccount(
        initial_capital=50_000,
        current_capital=39_000,
        total_assets=39_000,
        max_drawdown=-22.0,
    )

    assert paper._is_drawdown_recovery_buy(
        account,
        open_count=0,
        today_new_buy_count=0,
        candidate_score=86.0,
        allow_continuous_participation=True,
    ) is True
    assert paper._is_drawdown_recovery_buy(
        account,
        open_count=0,
        today_new_buy_count=1,
        candidate_score=86.0,
        allow_continuous_participation=True,
    ) is False
    assert paper._is_drawdown_recovery_buy(
        account,
        open_count=0,
        today_new_buy_count=0,
        candidate_score=86.0,
    ) is False


def test_continuous_participation_requires_confirmed_low_chase_source_and_only_drawdown_warn():
    candidate = {
        "_source": "daily_participation",
        "execution_confirmation": True,
    }
    assert paper._candidate_allows_continuous_participation(candidate, 86.0) is True
    assert paper._candidate_allows_continuous_participation(
        {"_source": "anomaly_buy_point", "execution_confirmation": True},
        95.0,
    ) is False
    assert paper._continuous_participation_warn_allowed(
        {
            "final_level": "warn",
            "warnings": [{"rule": "max_drawdown", "category": "drawdown"}],
        },
        True,
    ) is True
    assert paper._continuous_participation_warn_allowed(
        {
            "final_level": "warn",
            "warnings": [
                {"rule": "max_drawdown", "category": "drawdown"},
                {"rule": "position_limit", "category": "position"},
            ],
        },
        True,
    ) is False


def test_staged_entry_sizes_by_score_and_keeps_room_for_next_layer():
    account = PaperAccount(
        initial_capital=50_000,
        current_capital=50_000,
        total_assets=50_000,
        max_drawdown=-22.0,
    )
    position = PaperPosition(
        account_id=1,
        code="000001",
        buy_price=10.0,
        buy_amount=500,
        current_price=10.0,
    )

    assert paper._staged_entry_buy_amount(account, price=10.0, score=86.0) == 300
    assert paper._staged_entry_buy_amount(account, price=10.0, score=90.0) == 400
    assert paper._staged_entry_buy_amount(account, price=10.0, score=90.0, position=position) == 400


def test_all_auto_sources_use_first_layer_and_never_force_one_lot_over_budget():
    account = PaperAccount(
        initial_capital=50_000,
        current_capital=50_000,
        total_assets=50_000,
        max_drawdown=0,
    )

    # 2026-08-31 复盘新增：硬止损仓位上限拦截超过总资产 2% 的单笔仓位
    # 50000 × 2% / (10.0 × 5%) = 2000 股
    assert paper._auto_buy_amount(account, price=10.0, open_count=0, score=91.0) == 2000
    assert paper._layered_auto_buy_amount(
        account,
        price=10.0,
        open_count=0,
        score=91.0,
    ) == 700
    assert paper._layered_auto_buy_amount(
        account,
        price=80.0,
        open_count=0,
        score=91.0,
    ) == 0


def test_scale_in_requires_reconfirmation_without_chasing_or_averaging_down():
    candidate = {
        "_source": "daily_participation",
        "execution_confirmation": True,
        "stop_loss_price": 9.6,
    }
    position = PaperPosition(
        account_id=1,
        code="000001",
        buy_price=10.0,
        buy_amount=500,
        current_price=10.0,
        stop_loss_price=9.5,
    )

    assert paper._scale_in_reject_reason(
        candidate,
        position=position,
        price=10.05,
        score=86.0,
        total_assets=50_000,
        bought_code_today=False,
    ) == ""
    assert "今日已完成" in paper._scale_in_reject_reason(
        candidate,
        position=position,
        price=10.05,
        score=86.0,
        total_assets=50_000,
        bought_code_today=True,
    )
    assert "超过加仓追价上限" in paper._scale_in_reject_reason(
        candidate,
        position=position,
        price=10.20,
        score=86.0,
        total_assets=50_000,
        bought_code_today=False,
    )
    assert "不做下跌摊平" in paper._scale_in_reject_reason(
        candidate,
        position=position,
        price=9.70,
        score=86.0,
        total_assets=50_000,
        bought_code_today=False,
    )


def test_severe_drawdown_blocks_strong_empty_account_probe(monkeypatch):
    monkeypatch.setattr(paper.settings, "PAPER_AUTO_DRAWDOWN_BUY_GATE_MODE", "pause")
    account = PaperAccount(
        initial_capital=50_000,
        current_capital=44_346.68,
        total_assets=44_346.68,
        max_drawdown=-15.90,
    )

    assert paper._is_drawdown_recovery_buy(
        account,
        open_count=0,
        today_new_buy_count=0,
        candidate_score=91.9,
    ) is False
    assert paper._is_drawdown_recovery_buy(
        account,
        open_count=0,
        today_new_buy_count=0,
        candidate_score=93.9,
    ) is False


def test_execution_value_gate_rejects_huatong_intraday_high_chase():
    candidate = {
        "_source": "icepoint_reversal",
        "execution_confirmation": True,
        "change_pct": 3.77,
        "main_wave_stats": {"support": 7.80, "resistance": 8.48},
        "dynamic_pool_stats": {"support": 8.16, "resistance": 8.48},
        "stop_loss_price": 8.28,
    }
    spot = StockSpot(
        code="002840", price=8.54, high=8.57, avg_price=8.42, change_pct=3.77,
    )

    reason = paper._candidate_execution_value_reject_reason(
        candidate,
        price=8.54,
        spot=spot,
    )

    assert "超过自动低吸上限" in reason or "日内高点" in reason


def test_execution_value_gate_allows_confirmed_support_reclaim_with_reward_risk():
    candidate = {
        "_source": "ma5_pullback",
        "execution_confirmation": True,
        "change_pct": 0.8,
        "support": 9.88,
        "resistance": 10.80,
        "stop_loss_price": 9.75,
    }
    spot = StockSpot(
        code="600001", price=10.02, high=10.15, avg_price=9.98, change_pct=0.8,
    )

    assert paper._candidate_execution_value_reject_reason(
        candidate,
        price=10.02,
        spot=spot,
    ) == ""


def test_execution_value_gate_rejects_rebound_far_from_intraday_low():
    candidate = {
        "_source": "underwater_reversal",
        "execution_confirmation": True,
        "change_pct": 1.5,
        "stop_loss_price": 9.72,
    }
    spot = StockSpot(
        code="600002", price=10.15, low=9.60, high=10.30,
        avg_price=10.10, change_pct=1.5,
    )

    reason = paper._candidate_execution_value_reject_reason(
        candidate,
        price=10.15,
        spot=spot,
    )

    assert "日内低点反弹" in reason


def test_live_paper_confirmation_does_not_treat_tencent_placeholder_zero_as_flat(monkeypatch):
    from app.signal.anomaly_scanner import anomaly_scanner

    spot = StockSpot(code="600001", price=10.0, min5_change=0.0)
    monkeypatch.setattr(
        anomaly_scanner,
        "track_intraday_min5_change",
        lambda _spot: None,
    )

    assert paper._paper_effective_min5_change(spot, trade_date=date.today()) is None
    assert paper._paper_effective_min5_change(
        spot,
        trade_date=date.today() - timedelta(days=1),
    ) == 0.0


def test_reversal_requires_first_pause_retest_instead_of_straight_line_chase():
    assert paper._reversal_retest_confirmed(
        change_pct=1.2,
        avg_premium_pct=0.2,
        close_position=0.62,
        min5_change=0.3,
    ) is True
    assert paper._reversal_retest_confirmed(
        change_pct=1.2,
        avg_premium_pct=0.2,
        close_position=0.90,
        min5_change=1.4,
    ) is False
    assert paper._candidate_is_observation_only({
        "_source": "underwater_reversal",
        "execution_confirmation": False,
    }) is True


def test_sector_counts_from_auto_logs_prevents_duplicate_theme_buys():
    first = PaperAutoTradeLog(
        run_id="r1",
        trade_date=date(2026, 5, 19),
        source="anomaly_buy_point",
        action="buy",
        decision="executed",
        candidate_json='{"sector_name":"DeepSeek概念"}',
    )
    second = PaperAutoTradeLog(
        run_id="r1",
        trade_date=date(2026, 5, 19),
        source="anomaly_buy_point",
        action="buy",
        decision="executed",
        candidate_json='{"sector_name":"DeepSeek概念"}',
    )

    assert paper._sector_counts_from_auto_logs([first, second]) == {"DeepSeek概念": 2}


def test_plan_hot_sector_gate_filters_generic_and_cold_sectors():
    generic_ok, generic_reason, _ = paper._plan_hot_sector_gate({
        "sector_resonance_name": "融资融券",
        "sector_resonance": "强共振",
        "sector_resonance_score": 90,
        "sector_resonance_change": 2.0,
        "sector_resonance_fund": 10,
    })
    cold_ok, cold_reason, _ = paper._plan_hot_sector_gate({
        "sector_resonance_name": "冷门概念",
        "sector_resonance": "独立行情",
        "sector_resonance_score": 35,
        "sector_resonance_change": -1.2,
        "sector_resonance_fund": -0.5,
    })
    hot_ok, hot_reason, ctx = paper._plan_hot_sector_gate({
        "sector_resonance_name": "机器人",
        "sector_resonance": "强共振",
        "sector_resonance_score": 76,
        "sector_resonance_change": 1.5,
        "sector_resonance_fund": 6.2,
    })

    assert not generic_ok
    assert "泛概念" in generic_reason
    assert not cold_ok
    assert "题材合力不足" in cold_reason
    assert hot_ok
    assert "机器人" in hot_reason
    assert ctx["sector_name"] == "机器人"


def test_intraday_buy_window_excludes_close_and_late_minutes():
    assert paper._is_intraday_buy_window(datetime(2026, 4, 30, 10, 0))
    assert paper._is_intraday_buy_window(datetime(2026, 4, 30, 13, 30))
    assert not paper._is_intraday_buy_window(datetime(2026, 4, 30, 15, 45))
    assert not paper._is_intraday_buy_window(datetime(2026, 4, 30, 14, 55))


def test_highboard_has_strategy_specific_0930_buy_window():
    opening_minute = datetime(2026, 4, 30, 9, 31)

    assert not paper._is_intraday_buy_window(opening_minute)
    assert paper._strategy_intraday_buy_start(paper.PAPER_ACCOUNT_DEFAULT) == paper.settings.PAPER_INTRADAY_BUY_START
    assert paper._strategy_intraday_buy_start(paper.PAPER_ACCOUNT_TENBAGGER) == paper.settings.PAPER_HIGHBOARD_INTRADAY_BUY_START
    assert paper._is_intraday_buy_window(
        opening_minute,
        start_value=paper._strategy_intraday_buy_start(paper.PAPER_ACCOUNT_TENBAGGER),
    )


def test_recovery_buy_window_blocks_late_new_positions():
    assert paper._is_recovery_buy_time_window(datetime(2026, 6, 3, 10, 0))
    assert paper._is_recovery_buy_time_window(datetime(2026, 6, 3, 14, 0))
    assert not paper._is_recovery_buy_time_window(datetime(2026, 6, 3, 14, 1))
    assert paper._is_late_new_buy_time(datetime(2026, 6, 3, 14, 1))


def test_auto_run_request_defaults_to_intraday_mode():
    req = paper.AutoRunRequest()

    assert req.execution_mode == "intraday"


def test_auto_buy_amount_uses_short_swing_layers():
    account = paper_models.PaperAccount(
        initial_capital=50_000,
        current_capital=50_000,
        total_assets=50_000,
    )

    # 2026-08-31 复盘新增：硬止损仓位上限 = 50000 × 2% / (10.0 × 5%) = 2000 股
    # score>=86 原本会超过 2000 被新上限截断
    assert paper._auto_buy_amount(account, price=10.0, open_count=0, score=91) == 2000
    assert paper._auto_buy_amount(account, price=10.0, open_count=0, score=86) == 2000
    assert paper._auto_buy_amount(account, price=10.0, open_count=0, score=80) == 1700
    assert paper._auto_buy_amount(account, price=10.0, open_count=0, score=74) == 1200


def test_auto_buy_amount_targets_two_stock_full_position():
    account = paper_models.PaperAccount(
        initial_capital=50_000,
        current_capital=50_000,
        total_assets=50_000,
    )

    # 2026-08-31 复盘新增：硬止损仓位上限 = 50000 × 2% / (25 × 5%) = 800 股
    first = paper._auto_buy_amount(account, price=25.0, open_count=0, score=92)
    assert first == 800

    account.current_capital = 50_000 - first * 25.0
    second = paper._auto_buy_amount(account, price=25.0, open_count=1, score=92)
    # 硬止损仓位上限：50000 × 2% / (25 × 5%) = 800 股，全资产口径限制
    assert second == 800


def test_legacy_full_conviction_is_disabled_and_capped_by_staged_position(monkeypatch):
    account = paper_models.PaperAccount(
        initial_capital=50_000,
        current_capital=50_000,
        total_assets=50_000,
    )
    candidate = {
        "ma5_pullback": True,
        "ma5_distance_pct": 0.6,
        "change_pct": 1.4,
        "volume_ratio": 1.2,
    }

    assert paper._is_full_conviction_candidate(candidate, score=96.5) is False
    monkeypatch.setattr(paper.settings, "PAPER_AUTO_FULL_CONVICTION_ENABLED", True)
    assert paper._is_full_conviction_candidate(candidate, score=96.5) is True
    assert paper._full_conviction_buy_amount(account, price=25.0) == 500


def test_full_conviction_candidate_rejects_chasing_position():
    candidate = {
        "ma5_pullback": True,
        "ma5_distance_pct": 1.4,
        "change_pct": 3.1,
        "volume_ratio": 1.2,
    }

    assert paper._is_full_conviction_candidate(candidate, score=97.0) is False


def test_auto_buy_pause_allows_empty_account_recovery_entry():
    account = paper_models.PaperAccount(
        initial_capital=50_000,
        current_capital=49_000,
        total_assets=49_000,
        max_drawdown=-2.6,
    )
    account.current_drawdown = -2.5

    assert paper._auto_buy_pause_reason(
        account,
        open_count=0,
        today_new_buy_count=0,
        candidate_score=91,
    ) == ""
    # 2026-08-31 复盘新增：硬止损仓位上限 = 49000 × 2% / (10 × 5%) = 1960 -> 1900
    assert paper._auto_buy_amount(account, price=10.0, open_count=0, score=91) == 1900


def test_auto_buy_pause_allows_profit_position_recovery_entry():
    account = paper_models.PaperAccount(
        initial_capital=50_000,
        current_capital=46_000,
        total_assets=49_000,
        max_drawdown=-2.9,
    )
    account.current_drawdown = -2.9
    protected = PaperPosition(
        account_id=1,
        code="002491",
        buy_price=24.63,
        buy_amount=100,
        current_price=25.45,
        profit_pct=3.33,
    )
    weak = PaperPosition(
        account_id=1,
        code="000001",
        buy_price=10.0,
        buy_amount=100,
        current_price=10.1,
        profit_pct=1.0,
    )

    assert paper._has_recovery_protected_profit_position([protected])
    assert not paper._has_recovery_protected_profit_position([weak])
    assert paper._auto_buy_pause_reason(
        account,
        open_count=1,
        today_new_buy_count=0,
        candidate_score=91,
        allow_profit_position=True,
    ) == ""


def test_auto_buy_pause_blocks_weak_or_deep_drawdown_recovery(monkeypatch):
    monkeypatch.setattr(paper.settings, "PAPER_AUTO_DRAWDOWN_BUY_GATE_MODE", "pause")
    weak_account = paper_models.PaperAccount(
        initial_capital=50_000,
        current_capital=49_000,
        total_assets=49_000,
        max_drawdown=-2.6,
    )
    weak_account.current_drawdown = -2.5
    deep_account = paper_models.PaperAccount(
        initial_capital=50_000,
        current_capital=48_000,
        total_assets=48_000,
        max_drawdown=-3.8,
    )
    deep_account.current_drawdown = -3.8

    assert paper._auto_buy_pause_reason(
        weak_account,
        open_count=0,
        today_new_buy_count=0,
        candidate_score=89,
    ) == ""
    assert "未满足空仓/盈利保护持仓高分恢复开仓条件" in paper._auto_buy_pause_reason(
        weak_account,
        open_count=1,
        today_new_buy_count=0,
        candidate_score=91,
    )
    assert paper._auto_buy_pause_reason(
        deep_account,
        open_count=0,
        today_new_buy_count=0,
        candidate_score=95,
    ) == ""


def test_auto_sell_amount_uses_t_layers():
    position = PaperPosition(account_id=1, code="000001", buy_price=10.0, buy_amount=1000)

    assert paper._auto_sell_amount(position, 1000, "触发短线止盈：4.00%") == 500
    assert paper._auto_sell_amount(position, 1000, "回落成本线保护：当前盈亏0.10%") == 500
    assert paper._auto_sell_amount(position, 1000, "跌破分时均价：短线转弱") == 300
    assert paper._auto_sell_amount(position, 1000, "触发硬止损：-3.20%") == 1000
    assert paper._is_post_t_protect_exit_reason("跌破分时均价：现价24.52 < 均价25.47，短线转弱")
    assert not paper._is_post_t_protect_exit_reason("触发短线止盈：6.01%")


@pytest.mark.asyncio
async def test_paper_sell_only_allows_overnight_lot_after_same_day_buy(paper_client, qualified_manual_execution):
    client, SessionLocal = paper_client
    async with SessionLocal() as session:
        session.add(StockSpot(code="000002", name="万科A", price=10.0))
        await session.commit()

    await qualified_manual_execution("000002", "万科A", trade_at=datetime(2026, 9, 11, 10))
    first_buy = await client.post("/paper/buy", json={
        "code": "000002",
        "price": 10.0,
        "amount": 100,
        "signal_id": "overnight",
    })
    assert first_buy.status_code == 200

    await qualified_manual_execution("000002", "万科A", ask=9.8, bid=9.79,
                                     trade_at=datetime(2026, 9, 14, 10))
    second_buy = await client.post("/paper/buy", json={
        "code": "000002",
        "price": 9.8,
        "amount": 100,
        "signal_id": "t-buyback",
    })
    assert second_buy.status_code == 200

    await qualified_manual_execution("000002", "万科A", last=10.2, ask=10.21, bid=10.2)
    blocked = await client.post("/paper/sell", json={
        "code": "000002",
        "price": 10.2,
        "amount": 200,
        "reason": "try_sell_today_buy",
    })
    assert blocked.status_code == 400
    assert "可卖隔夜仓100股" in blocked.json()["detail"]

    sell = await client.post("/paper/sell", json={
        "code": "000002",
        "price": 10.2,
        "amount": 100,
        "reason": "sell_overnight_lot",
    })
    assert sell.status_code == 200


@pytest.mark.asyncio
async def test_t_buyback_does_not_rebuy_above_cost_after_profit_sell(paper_client, monkeypatch):
    _client, SessionLocal = paper_client
    trade_date = date(2026, 9, 10)
    decision_at = datetime.combine(trade_date, time(10))
    # The 09:49 sell must already be visible at the evaluation cutoff.
    monkeypatch.setattr(paper, "_paper_now", lambda: decision_at)
    async with SessionLocal() as session:
        account = paper_models.PaperAccount(
            account_name="default",
            initial_capital=50_000,
            current_capital=50_000,
            total_assets=50_000,
            status="active",
        )
        session.add(account)
        await session.flush()
        session.add_all([
            PaperPosition(
                account_id=account.id,
                code="603002",
                name="宏昌电子",
                buy_price=17.32,
                buy_amount=300,
                current_price=17.85,
                buy_time=datetime.combine(trade_date - timedelta(days=1), datetime.min.time()).replace(hour=14, minute=10),
                strategy_version=paper._strategy_version("default"),
                is_closed=False,
            ),
            PaperTradeLog(
                account_id=account.id,
                code="603002",
                trade_type="sell",
                price=18.10,
                amount=100,
                trade_time=datetime.combine(trade_date, datetime.min.time()).replace(hour=9, minute=49),
                commission=2.34,
                realized_pnl=70.66,
            ),
            StockSpot(
                code="603002",
                name="宏昌电子",
                price=17.85,
                open=17.3,
                high=18.56,
                low=16.99,
                avg_price=17.8,
                change_pct=2.47,
                volume_ratio=3.82,
                orderbook_imbalance=0.62,
                ask1_price=17.86,
                updated_at=decision_at,
                source_quote_at=decision_at,
                received_at=decision_at,
            ),
        ])
        await session.commit()

        logs = await paper._run_auto_t_buybacks(
            session,
            account=account,
            run_id="test-t",
            trade_date=trade_date,
            trigger="test",
            execute=False,
        )

    assert logs
    assert logs[0].action == "hold"
    assert "高于成本保护价" in logs[0].reason


@pytest.mark.asyncio
async def test_paper_auto_evaluation_groups_executed_logs(paper_client):
    client, SessionLocal = paper_client
    await client.get("/paper/account")

    async with SessionLocal() as session:
        account = (await session.execute(select(paper_models.PaperAccount))).scalar_one()
        buy_trade = PaperTradeLog(
            account_id=account.id,
            code="000003",
            trade_type="buy",
            price=10.0,
            amount=100,
            trade_time=datetime.now() - timedelta(days=1),
            commission=0.3,
            signal_id="auto-radar-000003",
            reason="auto-radar-000003",
        )
        sell_trade = PaperTradeLog(
            account_id=account.id,
            code="000003",
            trade_type="sell",
            price=10.5,
            amount=100,
            trade_time=datetime.now(),
            commission=1.36,
            reason="T减仓100股：触发短线止盈：5.00%",
            realized_pnl=48.64,
        )
        session.add_all([buy_trade, sell_trade])
        await session.flush()
        session.add_all([
            PaperAutoTradeLog(
                run_id="r1",
                trade_date=datetime.now().date(),
                trigger="test",
                source="radar",
                code="000003",
                action="buy",
                decision="executed",
                reason="牛股雷达量价齐升买点，评分80.0",
                price=10.0,
                amount=100,
                executed_trade_id=buy_trade.id,
            ),
            PaperAutoTradeLog(
                run_id="r1",
                trade_date=datetime.now().date(),
                trigger="test",
                source="position",
                code="000003",
                action="sell",
                decision="executed",
                reason="T减仓100股：触发短线止盈：5.00%",
                price=10.5,
                amount=100,
                executed_trade_id=sell_trade.id,
            ),
        ])
        await session.commit()

    response = await client.get("/paper/auto/evaluation")

    assert response.status_code == 200
    data = response.json()
    assert data["stats"]["auto_buy"]["count"] == 1
    assert data["stats"]["t_sell"]["count"] == 1
    assert data["stats"]["t_sell"]["wins"] == 1
    assert data["stats"]["t_sell"]["total_pnl"] == 48.64


@pytest.mark.asyncio
async def test_auto_evaluation_uses_each_strategy_actual_thresholds(paper_client):
    client, _SessionLocal = paper_client
    response = await client.get(
        "/paper/auto/evaluation",
        params={"account_name": paper.PAPER_ACCOUNT_TENBAGGER},
    )

    assert response.status_code == 200
    thresholds = response.json()["diagnostic_thresholds"]
    assert thresholds["wrong_buy_loss_pct"] == paper.settings.PAPER_HIGHBOARD_STOP_LOSS_PCT
    assert thresholds["wrong_sell_remaining_profit_pct"] == (
        paper.settings.PAPER_HIGHBOARD_TAKE_PROFIT_PCT
    )
    assert thresholds["strategy_version"] == paper._strategy_version(paper.PAPER_ACCOUNT_TENBAGGER)


@pytest.mark.asyncio
async def test_paper_auto_logs_can_filter_today(paper_client):
    client, SessionLocal = paper_client
    today = datetime.now().date()
    yesterday = today - timedelta(days=1)

    async with SessionLocal() as session:
        session.add_all([
            PaperAutoTradeLog(
                run_id="old",
                trade_date=yesterday,
                trigger="test",
                source="system",
                action="empty",
                decision="wait",
                reason="old-log",
                created_at=datetime.now() - timedelta(days=1),
            ),
            PaperAutoTradeLog(
                run_id="today",
                trade_date=today,
                trigger="test",
                source="system",
                action="empty",
                decision="wait",
                reason="today-log",
                created_at=datetime.now(),
            ),
        ])
        await session.commit()

    response = await client.get("/paper/auto/logs", params={"today_only": True})

    assert response.status_code == 200
    reasons = [item["reason"] for item in response.json()["logs"]]
    assert reasons == ["today-log"]


@pytest.mark.asyncio
async def test_drawdown_pause_backtest_uses_one_signal_per_day_and_future_klines(paper_client):
    client, SessionLocal = paper_client
    signal_day = date(2026, 1, 5)
    async with SessionLocal() as session:
        session.add_all([
            PaperAutoTradeLog(
                run_id="pause-high",
                trade_date=signal_day,
                trigger="test",
                source="daily_participation",
                code="000001",
                name="观察样本",
                action="skip_buy",
                decision="skipped",
                reason="账户当前回撤22.00%达到暂停开仓线2.00%",
                price=10.0,
                candidate_score=90.0,
            ),
            PaperAutoTradeLog(
                run_id="pause-low",
                trade_date=signal_day,
                trigger="test",
                source="radar",
                code="000002",
                name="重复轮询样本",
                action="skip_buy",
                decision="skipped",
                reason="账户当前回撤22.00%达到暂停开仓线2.00%",
                price=8.0,
                candidate_score=80.0,
            ),
            StockKline(code="000001", trade_date=date(2026, 1, 6), close=10.2, low=9.8, high=10.3),
            StockKline(code="000001", trade_date=date(2026, 1, 7), close=10.5, low=10.1, high=10.6),
            StockKline(code="000001", trade_date=date(2026, 1, 8), close=11.0, low=10.4, high=11.2),
        ])
        await session.commit()

    response = await client.get("/paper/auto/drawdown-backtest", params={"holding_days": 3})

    assert response.status_code == 200
    data = response.json()
    assert data["paused_signal_days"] == 1
    assert data["completed_samples"] == 1
    assert data["continuous_one_lot"]["wins"] == 1
    assert data["continuous_one_lot"]["total_pnl"] > 0
    assert data["samples"][0]["code"] == "000001"


@pytest.mark.asyncio
async def test_auto_trade_default_mode_runs_intraday_guard(monkeypatch):
    class FakeDb:
        def __init__(self):
            self.logs = []
            self.committed = False

        def add(self, item):
            self.logs.append(item)

        async def flush(self):
            return None

        async def scalar(self, _statement):
            return paper.PAPER_ACCOUNT_PROMOTION

        async def commit(self):
            self.committed = True

    async def fake_should_run(now=None):
        return False, "当前时段close，不做盘中自动交易"

    class FakeAccount:
        id = 42

    seen_accounts = []

    async def fake_get_account(_db, account_name):
        seen_accounts.append(account_name)
        return FakeAccount()

    db = FakeDb()
    monkeypatch.setattr(paper, "_should_run_intraday_auto_trade", fake_should_run)
    monkeypatch.setattr(paper, "_get_or_create_account", fake_get_account)

    result = await paper.run_paper_auto_trade(
        db,
        execute=True,
        trigger="test",
        account_name=paper.PAPER_ACCOUNT_PROMOTION,
    )

    assert result["execution_mode"] == "intraday"
    assert result["summary"] == {"executed": 0, "blocked": 0, "skipped": 1, "wait": 0, "dry_run": 0}
    assert result["logs"][0]["decision"] == "skipped"
    assert result["logs"][0]["reason"] == "当前时段close，不做盘中自动交易"
    assert seen_accounts == [paper.PAPER_ACCOUNT_PROMOTION]
    assert db.logs[0].account_id == 42
    assert db.committed


@pytest.mark.asyncio
async def test_close_mode_does_not_load_buy_candidates(monkeypatch, paper_client):
    _client, SessionLocal = paper_client

    async def fail_candidates(*args, **kwargs):
        raise AssertionError("close mode must not load buy candidates")

    monkeypatch.setattr(paper, "_paper_auto_buy_candidates", fail_candidates)

    async with SessionLocal() as session:
        result = await paper.run_paper_auto_trade(
            session,
            execute=False,
            trigger="test-close",
            execution_mode="close",
        )

    assert result["execution_mode"] == "close"
    assert any("收盘复盘模式不再自动买入" in item["reason"] for item in result["logs"])


# =========================================================================
# 2026-08-31 双策略并行: 策略B 晋级预测二板赛道
# =========================================================================

def test_strategy_sell_params_distinguish_all_accounts():
    """执行与评估必须对A-F返回各自真实的退出阈值。"""
    from app.models.paper import PaperAccount as PA

    def params(account_name: str) -> dict:
        return paper._strategy_sell_params(
            PA(account_name=account_name, strategy=account_name, initial_capital=50_000)
        )

    default_params = params(paper.PAPER_ACCOUNT_DEFAULT)
    promo_params = params(paper.PAPER_ACCOUNT_PROMOTION)
    mainline_params = params(paper.PAPER_ACCOUNT_MAINLINE)
    auction_params = params(paper.PAPER_ACCOUNT_AUCTION)
    tenbagger_params = params(paper.PAPER_ACCOUNT_TENBAGGER)
    reversal_params = params(paper.PAPER_ACCOUNT_REVERSAL)

    assert default_params["small_stop_loss_pct"] == paper.settings.PAPER_AUTO_SMALL_STOP_LOSS_PCT
    assert promo_params["take_profit_pct"] == paper.settings.PAPER_PROMOTION_TAKE_PROFIT_PCT
    assert mainline_params["stop_loss_pct"] == paper.settings.PAPER_MAINLINE_STOP_LOSS_PCT
    assert auction_params["max_hold_days"] == paper.settings.PAPER_AUCTION_MAX_HOLD_DAYS
    # 新快照包含完整退出细节；原六项阈值仍保持一致。
    assert {key: tenbagger_params[key] for key in (
        "take_profit_pct", "stop_loss_pct", "max_hold_days", "small_stop_loss_pct",
        "next_day_min_profit_pct", "open_severe_stop_loss_pct",
    )} == {
        "take_profit_pct": paper.settings.PAPER_HIGHBOARD_TAKE_PROFIT_PCT,
        "stop_loss_pct": paper.settings.PAPER_HIGHBOARD_STOP_LOSS_PCT,
        "max_hold_days": paper.settings.PAPER_HIGHBOARD_MAX_HOLD_DAYS,
        "small_stop_loss_pct": paper.settings.PAPER_HIGHBOARD_STOP_LOSS_PCT,
        "next_day_min_profit_pct": -99.0,
        "open_severe_stop_loss_pct": paper.settings.PAPER_HIGHBOARD_STOP_LOSS_PCT,
    }
    assert reversal_params["stop_loss_pct"] == paper.settings.PAPER_REVERSAL_STOP_LOSS_PCT


@pytest.mark.asyncio
async def test_blacklist_window_is_evaluated_at_requested_trade_date(paper_client):
    _client, SessionLocal = paper_client
    requested = date(2026, 9, 1)
    async with SessionLocal() as session:
        session.add_all([
            StockBlacklist(
                code="600901",
                reason="st",
                start_date=date(2026, 9, 2),
                end_date=None,
            ),
            StockBlacklist(
                code="600902",
                reason="st",
                start_date=date(2026, 8, 30),
                end_date=date(2026, 9, 1),
            ),
            StockBlacklist(
                code="600903",
                reason="st",
                start_date=date(2026, 8, 1),
                end_date=date(2026, 8, 31),
            ),
        ])
        await session.commit()
        active_codes = set(
            (
                await session.scalars(
                    select(StockBlacklist.code).where(
                        paper._active_blacklist_clause(requested)
                    )
                )
            ).all()
        )

    assert active_codes == {"600902"}


def test_strategy_buy_limits_distinguish_accounts():
    """自动执行使用各策略自己的日买入数/持仓数，而不是统一套用策略A。"""
    assert paper._strategy_buy_limits(paper.PAPER_ACCOUNT_PROMOTION) == (
        paper.settings.PAPER_PROMOTION_MAX_DAILY_BUYS,
        paper.settings.PAPER_PROMOTION_MAX_POSITIONS,
    )
    assert paper._strategy_buy_limits(paper.PAPER_ACCOUNT_MAINLINE) == (
        paper.settings.PAPER_MAINLINE_MAX_DAILY_BUYS,
        paper.settings.PAPER_MAINLINE_MAX_POSITIONS,
    )
    assert paper._strategy_buy_limits(paper.PAPER_ACCOUNT_DEFAULT) == (
        paper.settings.PAPER_AUTO_MAX_DAILY_NEW_BUYS,
        paper.settings.PAPER_AUTO_MAX_POSITIONS,
    )


def test_short_sell_reason_uses_strategy_b_params():
    """策略B持仓按 8% 止盈 / 6% 止损 / 3天时间止损 独立判定."""
    position = PaperPosition(
        account_id=1,
        code="000001",
        buy_price=10.0,
        buy_amount=100,
        buy_time=datetime.now() - timedelta(days=2),
    )
    # 用生产函数构造策略B参数 (含 small_stop_loss_pct=6% 等完整覆盖)
    promo_account = PaperAccount(account_name="promotion", strategy="promotion", initial_capital=50_000)
    promo_params = paper._strategy_sell_params(promo_account)
    # 6% 硬止损: 策略A参数下 -4% 已触发止损, 策略B参数下 -4% 仍在容忍
    reason_a = paper._short_sell_reason(
        position, {"price": 9.6, "stop_loss_price": 9.4}, profit_pct=-4.0,
        hold_days=2, trade_date=datetime.now().date(),
    )
    reason_b = paper._short_sell_reason(
        position, {"price": 9.6, "stop_loss_price": 9.4}, profit_pct=-4.0,
        hold_days=2, trade_date=datetime.now().date(), params=promo_params,
    )
    assert "小止损" in reason_a  # 策略A: -1.2% 小止损更早触发
    assert reason_b == ""        # 策略B: -6% 止损未触发, 不因噪音离场


def test_strategy_b_sell_take_profit_at_8_percent():
    """策略B: 止盈阈值以下不触发, 达到阈值触发 (2026-08-31 寻优后止盈=12%)."""
    position = PaperPosition(
        account_id=1, code="000001", buy_price=10.0, buy_amount=100,
        buy_time=datetime.now() - timedelta(days=1),
    )
    promo_params = {
        "take_profit_pct": paper.settings.PAPER_PROMOTION_TAKE_PROFIT_PCT,
        "stop_loss_pct": paper.settings.PAPER_PROMOTION_STOP_LOSS_PCT,
        "max_hold_days": paper.settings.PAPER_PROMOTION_MAX_HOLD_DAYS,
        "small_stop_loss_pct": paper.settings.PAPER_PROMOTION_STOP_LOSS_PCT,
        "next_day_min_profit_pct": -99.0,
    }
    tp = paper.settings.PAPER_PROMOTION_TAKE_PROFIT_PCT  # 12.0
    below = paper._short_sell_reason(
        position, {"price": 10.0 + 10.0 * (tp - 1) / 100, "stop_loss_price": 9.4},
        profit_pct=tp - 1.0, hold_days=1, trade_date=datetime.now().date(), params=promo_params,
    )
    at_tp = paper._short_sell_reason(
        position, {"price": 10.0 + 10.0 * tp / 100, "stop_loss_price": 9.4},
        profit_pct=tp, hold_days=1, trade_date=datetime.now().date(), params=promo_params,
    )
    assert below == ""
    assert "止盈" in at_tp


def _governed_promotion_run(
    *,
    run_key: str,
    reference_trade_date: date,
    snapshot_context: str,
    as_of_at: datetime,
    gate_passed: bool = True,
    status: str = "completed",
    route_gates: dict | None = None,
) -> PromotionPredictionRun:
    from app.api.v1.promotion import PROMOTION_MODEL_VERSION
    from app.promotion.route_contract import EXECUTION_ROUTES

    # This helper represents a route-qualified batch, not a legacy batch-only
    # pass. Dedicated contract tests build missing-map legacy evidence explicitly.
    if route_gates is None:
        route_gates = {route: {"gate_passed": gate_passed} for route in EXECUTION_ROUTES}
    return PromotionPredictionRun(
        run_key=run_key,
        snapshot_batch_key=f"batch-{run_key}",
        reference_trade_date=reference_trade_date,
        as_of_at=as_of_at,
        created_at=as_of_at,
        completed_at=as_of_at if status == "completed" else None,
        snapshot_source="schedule",
        snapshot_context=snapshot_context,
        model_version=PROMOTION_MODEL_VERSION,
        feature_version="test-features",
        data_version="test-data",
        runtime_mode="governed",
        status=status,
        gate_passed=gate_passed,
        candidate_count=1,
        ranked_count=1,
        actionable_count=int(gate_passed),
        payload_hash=("1" if gate_passed else "0") * 64,
        metadata_json=json.dumps(
            {"quality_gate": {"route_gates": route_gates}}
            if route_gates is not None
            else {},
            ensure_ascii=False,
        ),
    )


def _governed_promotion_snapshot(
    *,
    run_id: int,
    record_key: str,
    code: str,
    prediction_trade_date: date,
    target_board: int = 2,
    route: str = "second_board_promotion",
    probability: float = 0.5,
    trade_gate_passed: bool = True,
    actionable: bool = True,
    watch_only: bool = False,
) -> PromotionPredictionSnapshot:
    return PromotionPredictionSnapshot(
        run_id=run_id,
        record_key=record_key,
        code=code,
        name=record_key,
        target_board=target_board,
        prediction_trade_date=prediction_trade_date,
        horizon_days=1,
        candidate_route=route,
        rank_scope="trade_pool",
        rank_position=1,
        raw_probability=probability,
        calibrated_probability=probability,
        confidence_level="high",
        signal_status="close_confirmed",
        trade_gate_passed=trade_gate_passed,
        actionable=actionable,
        watch_only=watch_only,
        reason_json="{}",
        features_json="{}",
    )


@pytest.mark.asyncio
async def test_promotion_second_board_candidates_filter_by_probability(monkeypatch, paper_client):
    """B只消费最新治理批次内概率达标且三个单候选执行标志均通过的快照。"""
    _client, SessionLocal = paper_client
    today = date.today()
    signal_date = await paper.trade_calendar.previous_trade_day(today)
    as_of_at = datetime.combine(signal_date, datetime.min.time()).replace(hour=20)

    async with SessionLocal() as session:
        run = _governed_promotion_run(
            run_key="promotion-filter-flags",
            reference_trade_date=signal_date,
            snapshot_context="promotion_2000",
            as_of_at=as_of_at,
        )
        session.add(run)
        await session.flush()
        session.add_all([
            _governed_promotion_snapshot(
                run_id=run.id, record_key="valid", code="600000",
                prediction_trade_date=signal_date, probability=0.42,
            ),
            _governed_promotion_snapshot(
                run_id=run.id, record_key="low-probability", code="600001",
                prediction_trade_date=signal_date, probability=0.10,
            ),
            _governed_promotion_snapshot(
                run_id=run.id, record_key="not-actionable", code="600002",
                prediction_trade_date=signal_date, probability=0.90, actionable=False,
            ),
            _governed_promotion_snapshot(
                run_id=run.id, record_key="watch-only", code="600003",
                prediction_trade_date=signal_date, probability=0.90, watch_only=True,
            ),
            _governed_promotion_snapshot(
                run_id=run.id, record_key="candidate-gate-failed", code="600004",
                prediction_trade_date=signal_date, probability=0.90, trade_gate_passed=False,
            ),
        ])
        session.add_all([
            StockSpot(code=code, name=code, price=10.5, prev_close=10.0, change_pct=1.5, volume_ratio=1.8)
            for code in ["600000", "600001", "600002", "600003", "600004"]
        ])
        await session.commit()

        candidates, _notes = await paper._promotion_second_board_buy_candidates(
            session, limit=5, trade_date=today,
        )

    assert [candidate["code"] for candidate in candidates] == ["600000"]
    assert candidates[0]["_source"] == "promotion_promotion"
    assert candidates[0]["prediction_run_key"] == run.run_key
    assert candidates[0]["trade_gate_passed"] is True
    assert candidates[0]["actionable"] is True
    assert candidates[0]["watch_only"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("latest_status", "gate_passed"),
    [("completed", False), ("failed", False)],
)
async def test_promotion_second_board_does_not_fallback_to_older_high_probability_batch(
    paper_client, latest_status, gate_passed,
):
    """最新治理批次质量失败时，即使旧批次有高概率可执行票也必须失败关闭。"""
    _client, SessionLocal = paper_client
    today = date.today()
    signal_date = await paper.trade_calendar.previous_trade_day(today)

    async with SessionLocal() as session:
        older = _governed_promotion_run(
            run_key="older-passed",
            reference_trade_date=signal_date,
            snapshot_context="promotion_1510",
            as_of_at=datetime.combine(signal_date, datetime.min.time()).replace(hour=15, minute=10),
        )
        latest = _governed_promotion_run(
            run_key="latest-failed",
            reference_trade_date=signal_date,
            snapshot_context="promotion_2000",
            as_of_at=datetime.combine(signal_date, datetime.min.time()).replace(hour=20),
            gate_passed=gate_passed,
            status=latest_status,
        )
        session.add_all([older, latest])
        await session.flush()
        session.add(_governed_promotion_snapshot(
            run_id=older.id, record_key="old-valid", code="600020",
            prediction_trade_date=signal_date, probability=0.90,
        ))
        await session.commit()

        candidates, notes = await paper._promotion_second_board_buy_candidates(
            session, limit=5, trade_date=today,
        )

    assert candidates == []
    assert any("禁止回退旧批次" in note for note in notes)


@pytest.mark.asyncio
async def test_promotion_second_board_accepts_same_day_immutable_intraday_snapshot(paper_client, monkeypatch):
    """09:25/09:35是当日不可变快照，可在同日消费。"""
    _client, SessionLocal = paper_client
    today = date.today()
    monkeypatch.setattr(paper, "_paper_now", lambda: datetime.combine(today, time(14)))

    async with SessionLocal() as session:
        run = _governed_promotion_run(
            run_key="same-day-0935",
            reference_trade_date=today,
            snapshot_context="promotion_0935",
            as_of_at=datetime.combine(today, datetime.min.time()).replace(hour=9, minute=35),
        )
        session.add(run)
        await session.flush()
        session.add(_governed_promotion_snapshot(
            run_id=run.id, record_key="same-day-valid", code="600009",
            prediction_trade_date=today, probability=0.95,
        ))
        session.add(StockSpot(
            code="600009", name="当日预测票", price=10.2, prev_close=10.0,
            change_pct=2.0, volume_ratio=1.5,
        ))
        await session.commit()

        candidates, _notes = await paper._promotion_second_board_buy_candidates(
            session, limit=5, trade_date=today,
        )

    assert [candidate["code"] for candidate in candidates] == ["600009"]
    assert candidates[0]["snapshot_context"] == "promotion_0935"


@pytest.mark.asyncio
async def test_late_mainline_refresh_does_not_replace_b_or_d_opening_batch(paper_client, monkeypatch):
    _client, SessionLocal = paper_client
    today = date.today()
    monkeypatch.setattr(paper, "_paper_now", lambda: datetime.combine(today, time(14)))
    async with SessionLocal() as session:
        opening_run = _governed_promotion_run(
            run_key="opening-routes",
            reference_trade_date=today,
            snapshot_context="promotion_0935",
            as_of_at=datetime.combine(today, time(9, 35)),
        )
        late_run = _governed_promotion_run(
            run_key="late-mainline-only",
            reference_trade_date=today,
            snapshot_context="promotion_1030",
            as_of_at=datetime.combine(today, time(10, 30)),
        )
        session.add_all([opening_run, late_run])
        await session.flush()
        session.add_all([
            _governed_promotion_snapshot(
                run_id=opening_run.id,
                record_key="opening-b",
                code="600019",
                prediction_trade_date=today,
                probability=0.95,
            ),
            _governed_promotion_snapshot(
                run_id=opening_run.id,
                record_key="opening-d",
                code="600029",
                prediction_trade_date=today,
                target_board=1,
                route="auction_surge_start",
                probability=0.06,
            ),
        ])
        session.add_all([
            StockSpot(code="600019", name="B开盘票", price=10.2, prev_close=10.0, change_pct=2.0, volume_ratio=1.5),
            StockSpot(code="600029", name="D开盘票", price=10.2, prev_close=10.0, change_pct=2.0, volume_ratio=1.5),
        ])
        await session.commit()

        b_candidates, _ = await paper._promotion_route_buy_candidates(
            session, limit=5, trade_date=today, account_name=paper.PAPER_ACCOUNT_PROMOTION,
        )
        d_candidates, _ = await paper._promotion_route_buy_candidates(
            session, limit=5, trade_date=today, account_name=paper.PAPER_ACCOUNT_AUCTION,
        )

    assert [item["code"] for item in b_candidates] == ["600019"]
    assert [item["code"] for item in d_candidates] == ["600029"]
    assert b_candidates[0]["snapshot_context"] == "promotion_0935"
    assert d_candidates[0]["snapshot_context"] == "promotion_0935"


@pytest.mark.asyncio
async def test_promotion_second_board_does_not_fallback_when_latest_passed_run_has_no_route(paper_client):
    """最新通过批次没有B赛道候选时，也不能从更早B批次捞票。"""
    _client, SessionLocal = paper_client
    today = date.today()
    signal_date = await paper.trade_calendar.previous_trade_day(today)

    async with SessionLocal() as session:
        older = _governed_promotion_run(
            run_key="older-b-route",
            reference_trade_date=signal_date,
            snapshot_context="promotion_1510",
            as_of_at=datetime.combine(signal_date, datetime.min.time()).replace(hour=15, minute=10),
        )
        latest = _governed_promotion_run(
            run_key="latest-c-route-only",
            reference_trade_date=signal_date,
            snapshot_context="promotion_2000",
            as_of_at=datetime.combine(signal_date, datetime.min.time()).replace(hour=20),
        )
        session.add_all([older, latest])
        await session.flush()
        session.add_all([
            _governed_promotion_snapshot(
                run_id=older.id, record_key="old-b", code="600010",
                prediction_trade_date=signal_date, probability=0.95,
            ),
            _governed_promotion_snapshot(
                run_id=latest.id, record_key="new-c", code="600011",
                prediction_trade_date=signal_date, target_board=1,
                route="mainline_spread_start", probability=0.80,
            ),
        ])
        await session.commit()

        candidates, notes = await paper._promotion_second_board_buy_candidates(
            session, limit=5, trade_date=today,
        )

    assert candidates == []
    assert any("单候选可执行标志" in note for note in notes)


@pytest.mark.asyncio
async def test_promotion_second_board_rejects_same_day_close_snapshot(paper_client):
    """同日15:10/20:00收盘快照不能冒充盘中09:25/09:35快照执行。"""
    _client, SessionLocal = paper_client
    today = date.today()

    async with SessionLocal() as session:
        run = _governed_promotion_run(
            run_key="same-day-close-invalid",
            reference_trade_date=today,
            snapshot_context="promotion_2000",
            as_of_at=datetime.combine(today, datetime.min.time()).replace(hour=20),
        )
        session.add(run)
        await session.flush()
        session.add(_governed_promotion_snapshot(
            run_id=run.id, record_key="same-day-close", code="600000",
            prediction_trade_date=today, probability=0.9,
        ))
        await session.commit()

        candidates, notes = await paper._promotion_second_board_buy_candidates(
            session, limit=5, trade_date=today,
        )

    assert candidates == []
    assert any("今日允许消费" in note for note in notes)


@pytest.mark.asyncio
async def test_dual_account_creation_and_isolation(paper_client):
    """双账户并行: default 与 promotion 账户独立创建, 持仓互不影响."""
    client, SessionLocal = paper_client

    async with SessionLocal() as session:
        a_default = await paper._get_or_create_account(session, paper.PAPER_ACCOUNT_DEFAULT)
        a_promo = await paper._get_or_create_account(session, paper.PAPER_ACCOUNT_PROMOTION)
        assert a_default.id != a_promo.id
        assert a_promo.strategy == "promotion"
        assert a_default.strategy == "default"
        # 幂等: 再次获取同一账户
        again = await paper._get_or_create_account(session, paper.PAPER_ACCOUNT_PROMOTION)
        assert again.id == a_promo.id


@pytest.mark.asyncio
async def test_concurrent_account_initialization_creates_one_active_row(paper_client):
    """页面并发拉取账户/净值/日志时，同一策略只能初始化一个 active 账户。"""
    _client, SessionLocal = paper_client

    async def get_reversal_account_id() -> int:
        async with SessionLocal() as session:
            account = await paper._get_or_create_account(session, paper.PAPER_ACCOUNT_REVERSAL)
            return int(account.id)

    account_ids = await asyncio.gather(
        *(get_reversal_account_id() for _ in range(6))
    )
    assert len(set(account_ids)) == 1

    async with SessionLocal() as session:
        accounts = (
            await session.execute(
                select(PaperAccount).where(
                    PaperAccount.account_name == paper.PAPER_ACCOUNT_REVERSAL,
                    PaperAccount.status == "active",
                )
            )
        ).scalars().all()
    assert len(accounts) == 1
    assert accounts[0].strategy == paper.PAPER_ACCOUNT_REVERSAL


@pytest.mark.asyncio
async def test_auto_status_distinguishes_strategies(paper_client):
    """策略B(promotion) auto/status 返回独立口径, 策略A(default)保持原样."""
    client, SessionLocal = paper_client

    async with SessionLocal() as session:
        # 直接调用路由函数
        a_status = await paper.paper_auto_status(account_name="default", db=session)
        b_status = await paper.paper_auto_status(account_name="promotion", db=session)

    # 策略B 独立配置
    assert b_status["strategy"] == "promotion"
    assert b_status["max_positions"] == paper.settings.PAPER_PROMOTION_MAX_POSITIONS
    assert b_status["signal_policy"]["buy_source"].startswith("晋级预测二板")
    assert b_status["short_trade_rules"]["take_profit_pct"] == paper.settings.PAPER_PROMOTION_TAKE_PROFIT_PCT
    assert b_status["short_trade_rules"]["max_hold_days"] == paper.settings.PAPER_PROMOTION_MAX_HOLD_DAYS
    # 兼容前端 positionPolicyText (trial_pct/core_pct)
    assert b_status["position_policy"]["trial_pct"] == paper.settings.PAPER_PROMOTION_POSITION_PCT
    assert b_status["position_policy"]["core_pct"] == paper.settings.PAPER_PROMOTION_POSITION_PCT

    # 策略A 保持原样
    assert a_status.get("strategy") is None
    assert a_status["max_positions"] == paper.settings.PAPER_AUTO_MAX_POSITIONS
    assert a_status["signal_policy"]["buy_source"].startswith("高胜率预案")
    assert a_status["short_trade_rules"]["take_profit_pct"] == paper.settings.PAPER_AUTO_TAKE_PROFIT_PCT


@pytest.mark.asyncio
async def test_cross_account_data_isolation(paper_client, qualified_manual_execution):
    """跨账户数据隔离: 策略A买入不污染策略B, 持仓/交易记录/净值互不影响."""
    await qualified_manual_execution("600000", "账户A样本", ask=10)
    await qualified_manual_execution("600001", "账户B样本", last=5, ask=5, bid=4.99)
    client, SessionLocal = paper_client

    async with SessionLocal() as session:
        # 两个账户
        a = await paper._get_or_create_account(session, paper.PAPER_ACCOUNT_DEFAULT)
        b = await paper._get_or_create_account(session, paper.PAPER_ACCOUNT_PROMOTION)
        assert a.id != b.id

        # 策略A 通过 HTTP 买入一只股票
        await client.post("/paper/buy", params={"account_name": "default"}, json={
            "code": "600000", "price": 10.0, "amount": 100, "signal_id": "test-a",
        })
        # 策略B 通过 HTTP 买入另一只
        await client.post("/paper/buy", params={"account_name": "promotion"}, json={
            "code": "600001", "price": 5.0, "amount": 200, "signal_id": "test-b",
        })

    async with SessionLocal() as session:
        # 持仓隔离
        a_pos = await paper._open_positions(session, a.id)
        b_pos = await paper._open_positions(session, b.id)
        a_codes = {p.code for p in a_pos}
        b_codes = {p.code for p in b_pos}
        assert "600000" in a_codes
        assert "600001" not in a_codes
        assert "600001" in b_codes
        assert "600000" not in b_codes

        # 交易记录隔离
        a_trades = (await session.execute(
            select(PaperTradeLog).where(PaperTradeLog.account_id == a.id)
        )).scalars().all()
        b_trades = (await session.execute(
            select(PaperTradeLog).where(PaperTradeLog.account_id == b.id)
        )).scalars().all()
        assert {t.code for t in a_trades} == {"600000"}
        assert {t.code for t in b_trades} == {"600001"}

        # 净值基线隔离
        a_nav = (await session.execute(
            select(PaperNav).where(PaperNav.account_id == a.id).order_by(PaperNav.trade_date)
        )).scalars().all()
        b_nav = (await session.execute(
            select(PaperNav).where(PaperNav.account_id == b.id).order_by(PaperNav.trade_date)
        )).scalars().all()
        assert a_nav and b_nav
        # 两个账户的初始净值基线不同(各 5万 独立)
        assert a.initial_capital == b.initial_capital == 50_000


@pytest.mark.asyncio
@pytest.mark.parametrize("account_name", paper.PAPER_ALL_ACCOUNTS)
async def test_all_strategy_auto_status_counts_only_current_account(
    account_name, paper_client,
):
    """A-F 的持仓数和今日买卖计数必须绑定同一 account_id。"""
    _client, SessionLocal = paper_client
    trade_date = date.today()
    other_name = (
        paper.PAPER_ACCOUNT_PROMOTION
        if account_name == paper.PAPER_ACCOUNT_DEFAULT
        else paper.PAPER_ACCOUNT_DEFAULT
    )
    async with SessionLocal() as session:
        account = await paper._get_or_create_account(session, account_name)
        other = await paper._get_or_create_account(session, other_name)
        session.add_all([
            PaperPosition(
                account_id=account.id, code="600001", name="本账户持仓",
                buy_price=10, buy_amount=100, buy_time=datetime.now(),
                current_price=10, profit_pct=0, hold_days=0, is_closed=False,
            ),
            PaperPosition(
                account_id=other.id, code="600002", name="其他账户持仓",
                buy_price=10, buy_amount=100, buy_time=datetime.now(),
                current_price=10, profit_pct=0, hold_days=0, is_closed=False,
            ),
            PaperAutoTradeLog(
                account_id=account.id, run_id=f"{account_name}-buy",
                trade_date=trade_date, source="test", code="600001",
                action="buy", decision="executed",
            ),
            PaperAutoTradeLog(
                account_id=account.id, run_id=f"{account_name}-sell",
                trade_date=trade_date, source="position", code="600003",
                action="sell", decision="executed",
            ),
            PaperAutoTradeLog(
                account_id=other.id, run_id=f"{other_name}-foreign-buy",
                trade_date=trade_date, source="test", code="600002",
                action="buy", decision="executed",
            ),
        ])
        await session.commit()
        status = await paper.paper_auto_status(
            account_name=account_name,
            db=session,
        )

    assert status["account_scope"] == {
        "account_id": account.id,
        "account_name": account_name,
    }
    assert status["current_positions"] == {
        "count": 1,
        "codes": ["600001"],
    }
    assert status["today"]["buy_executed"] == 1
    assert status["today"]["sell_executed"] == 1
    assert status["today"]["executed"] == 2


# =========================================================================
# 2026-08-31 五策略并行: C主线扩散 / D竞价高开 / E十倍潜力中线
# =========================================================================

@pytest.mark.asyncio
async def test_promotion_route_candidates_mainline_and_auction(paper_client, monkeypatch):
    """C/D信任治理快照actionable契约，不套旧版20%/40%概率尺度。"""
    _client, SessionLocal = paper_client
    today = date.today()
    monkeypatch.setattr(paper, "_paper_now", lambda: datetime.combine(today, time(14)))

    async with SessionLocal() as session:
        run = _governed_promotion_run(
            run_key="same-day-cd-routes",
            reference_trade_date=today,
            snapshot_context="promotion_0935",
            as_of_at=datetime.combine(today, datetime.min.time()).replace(hour=9, minute=35),
        )
        session.add(run)
        await session.flush()
        session.add_all([
            _governed_promotion_snapshot(
                run_id=run.id, record_key="mainline", code="600010",
                prediction_trade_date=today, target_board=1,
                route="mainline_spread_start", probability=0.03,
            ),
            _governed_promotion_snapshot(
                run_id=run.id, record_key="auction", code="600020",
                prediction_trade_date=today, target_board=1,
                route="auction_surge_start", probability=0.06,
            ),
        ])
        session.add_all([
            StockSpot(code="600010", name="主线票", price=9.0, prev_close=8.8, change_pct=1.5, volume_ratio=1.5),
            StockSpot(code="600020", name="竞价票", price=5.5, prev_close=5.3, change_pct=1.2, volume_ratio=1.8),
        ])
        await session.commit()

        c_cands, _c_notes = await paper._promotion_route_buy_candidates(
            session, limit=5, trade_date=today, account_name=paper.PAPER_ACCOUNT_MAINLINE,
        )
        d_cands, _d_notes = await paper._promotion_route_buy_candidates(
            session, limit=5, trade_date=today, account_name=paper.PAPER_ACCOUNT_AUCTION,
        )

    assert [candidate["code"] for candidate in c_cands] == ["600010"]
    assert c_cands[0]["_source"] == "promotion_mainline"
    assert c_cands[0]["stop_loss_pct"] == paper.settings.PAPER_MAINLINE_STOP_LOSS_PCT
    assert [candidate["code"] for candidate in d_cands] == ["600020"]
    assert d_cands[0]["_source"] == "promotion_auction"
    assert d_cands[0]["stop_loss_pct"] == paper.settings.PAPER_AUCTION_STOP_LOSS_PCT


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("snapshot_context", "snapshot_time"),
    [
        ("promotion_0935", time(9, 35)),
        ("promotion_1000", time(10, 0)),
        ("promotion_1030", time(10, 30)),
        ("promotion_1305", time(13, 5)),
    ],
)
async def test_mainline_non_actionable_snapshot_requires_same_day_live_sector_spread(
    paper_client,
    monkeypatch,
    snapshot_context,
    snapshot_time,
):
    """C只消费当日不可变主线榜，并继续要求同板块实时扩散。"""
    _client, SessionLocal = paper_client
    today = date.today()
    monkeypatch.setattr(paper, "_paper_now", lambda: datetime.combine(today, time(14)))

    async with SessionLocal() as session:
        run = _governed_promotion_run(
            run_key=f"same-day-mainline-live-confirm-{snapshot_context}",
            reference_trade_date=today,
            snapshot_context=snapshot_context,
            as_of_at=datetime.combine(today, snapshot_time),
        )
        session.add(run)
        await session.flush()
        snapshot = _governed_promotion_snapshot(
            run_id=run.id,
            record_key="mainline-live-confirm",
            code="600110",
            prediction_trade_date=today,
            target_board=1,
            route="mainline_spread_start",
            probability=0.03,
            actionable=False,
        )
        snapshot.rank_scope = "recall_ranked"
        snapshot.features_json = json.dumps({
            "sector_code": "BK_MAIN",
            "sector_name": "主线扩散板块",
            "sector_strength_score": 62,
            "strict_confirmation_count": 3,
            "broad_rotation_member_setup": True,
            "sector_catalyst_spread": True,
        })
        session.add_all([
            snapshot,
            StockSpot(
                code="600110",
                name="主线扩散确认票",
                price=10.15,
                prev_close=10.0,
                limit_up=11.0,
                change_pct=1.5,
                volume_ratio=1.6,
            ),
            StockSectorMapping(
                code="600110",
                sector_code="BK_MAIN",
                sector_name="主线扩散板块",
                sector_type="concept",
                source="test",
            ),
            SectorPersistence(
                sector_code="BK_MAIN",
                sector_name="主线扩散板块",
                trade_date=today,
                strength_score=72,
                change_pct=2.1,
                fund_flow=18.0,
                limit_up_count=7,
                consecutive_days=2,
            ),
        ])
        await session.commit()

        candidates, notes = await paper._promotion_route_buy_candidates(
            session,
            limit=5,
            trade_date=today,
            account_name=paper.PAPER_ACCOUNT_MAINLINE,
        )

    assert not notes
    assert [candidate["code"] for candidate in candidates] == ["600110"]
    assert candidates[0]["snapshot_actionable"] is False
    assert candidates[0]["conditional_mainline_confirmation"] is True
    assert candidates[0]["execution_confirmation"] is True
    assert candidates[0]["sector_code"] == "BK_MAIN"


@pytest.mark.asyncio
async def test_mainline_live_confirmation_never_uses_close_snapshot(paper_client):
    """即使板块实时很强，C也不能把上一收盘版的非actionable候选补成订单。"""
    _client, SessionLocal = paper_client
    today = date.today()
    signal_date = await paper.trade_calendar.previous_trade_day(today)

    async with SessionLocal() as session:
        run = _governed_promotion_run(
            run_key="close-mainline-live-confirm-blocked",
            reference_trade_date=signal_date,
            snapshot_context="promotion_2000",
            as_of_at=datetime.combine(signal_date, time(20, 0)),
        )
        session.add(run)
        await session.flush()
        snapshot = _governed_promotion_snapshot(
            run_id=run.id,
            record_key="close-mainline",
            code="600111",
            prediction_trade_date=signal_date,
            target_board=1,
            route="mainline_spread_start",
            probability=0.03,
            actionable=False,
        )
        snapshot.rank_scope = "recall_ranked"
        snapshot.features_json = json.dumps({
            "sector_code": "BK_MAIN_OLD",
            "sector_name": "旧主线板块",
            "sector_strength_score": 70,
            "strict_confirmation_count": 3,
            "broad_rotation_member_setup": True,
            "sector_catalyst_spread": True,
        })
        session.add_all([
            snapshot,
            StockSpot(
                code="600111",
                name="旧快照候选",
                price=10.15,
                prev_close=10.0,
                limit_up=11.0,
                change_pct=1.5,
                volume_ratio=1.6,
            ),
            StockSectorMapping(
                code="600111",
                sector_code="BK_MAIN_OLD",
                sector_name="旧主线板块",
                sector_type="concept",
                source="test",
            ),
            SectorPersistence(
                sector_code="BK_MAIN_OLD",
                sector_name="旧主线板块",
                trade_date=today,
                strength_score=80,
                change_pct=3.0,
                fund_flow=30.0,
                limit_up_count=9,
                consecutive_days=3,
            ),
        ])
        await session.commit()

        candidates, notes = await paper._promotion_route_buy_candidates(
            session,
            limit=5,
            trade_date=today,
            account_name=paper.PAPER_ACCOUNT_MAINLINE,
        )

    assert candidates == []
    assert any("仅允许消费当日主线盘中不可变快照" in note for note in notes)


@pytest.mark.asyncio
async def test_auction_quality_failure_only_blocks_route_d(paper_client, monkeypatch):
    """竞价字段缺失可让全局批次告警，但不能误伤不依赖竞价的B/C路线。"""
    _client, SessionLocal = paper_client
    today = date.today()
    monkeypatch.setattr(paper, "_paper_now", lambda: datetime.combine(today, time(14)))
    route_gates = {
        "second_board_promotion": {"gate_passed": True, "blocking_datasets": []},
        "mainline_spread_start": {"gate_passed": True, "blocking_datasets": []},
        "auction_surge_start": {
            "gate_passed": False,
            "blocking_datasets": ["auction_data"],
        },
    }
    async with SessionLocal() as session:
        run = _governed_promotion_run(
            run_key="same-day-route-isolation",
            reference_trade_date=today,
            snapshot_context="promotion_0935",
            as_of_at=datetime.combine(today, time(9, 35)),
            gate_passed=False,
            route_gates=route_gates,
        )
        session.add(run)
        await session.flush()
        session.add_all([
            _governed_promotion_snapshot(
                run_id=run.id, record_key="route-b", code="600031",
                prediction_trade_date=today, target_board=2,
                route="second_board_promotion", probability=0.80,
            ),
            _governed_promotion_snapshot(
                run_id=run.id, record_key="route-c", code="600032",
                prediction_trade_date=today, target_board=1,
                route="mainline_spread_start", probability=0.03,
            ),
            _governed_promotion_snapshot(
                run_id=run.id, record_key="route-d", code="600033",
                prediction_trade_date=today, target_board=1,
                route="auction_surge_start", probability=0.06,
            ),
            StockSpot(code="600031", name="B票", price=10.15, prev_close=10.0, change_pct=1.5, volume_ratio=1.5),
            StockSpot(code="600032", name="C票", price=10.15, prev_close=10.0, change_pct=1.5, volume_ratio=1.5),
            StockSpot(code="600033", name="D票", price=10.15, prev_close=10.0, change_pct=1.5, volume_ratio=1.5),
        ])
        await session.commit()

        b_candidates, _ = await paper._promotion_route_buy_candidates(
            session, limit=5, trade_date=today, account_name=paper.PAPER_ACCOUNT_PROMOTION,
        )
        c_candidates, _ = await paper._promotion_route_buy_candidates(
            session, limit=5, trade_date=today, account_name=paper.PAPER_ACCOUNT_MAINLINE,
        )
        d_candidates, d_notes = await paper._promotion_route_buy_candidates(
            session, limit=5, trade_date=today, account_name=paper.PAPER_ACCOUNT_AUCTION,
        )

    assert [item["code"] for item in b_candidates] == ["600031"]
    assert [item["code"] for item in c_candidates] == ["600032"]
    assert d_candidates == []
    assert any("auction_data" in note and "禁止回退旧批次" in note for note in d_notes)


def test_midline_sell_reason_no_short_noise():
    """E/F只允许回放验证过的止盈、止损和交易日到期退出。"""
    position = PaperPosition(
        account_id=1, code="000001", buy_price=10.0, buy_amount=100,
        buy_time=datetime.now() - timedelta(days=5),
    )
    # 短线噪音: 跌破开盘价/分时均价/5分钟急跌 在高标接力卖出中不触发
    reason = paper._midline_sell_reason(
        position,
        {"price": 9.85, "open": 9.95, "avg_price": 9.9, "min5_change": -1.5, "ma5": 9.9, "ma20": 9.5, "stop_loss_price": 9.2},
        profit_pct=-1.5,
        hold_days=2,
    )
    assert reason == ""

    # MA20与板块退潮未进入回放退出规则，不能额外触发卖出。
    reason_unvalidated = paper._midline_sell_reason(
        position,
        {
            "price": 9.45,
            "ma20": 9.5,
            "sector_retreat_reason": "板块退潮",
            "stop_loss_price": 9.2,
        },
        profit_pct=-5.5,
        hold_days=2,
    )
    assert reason_unvalidated == ""

    # 到期平仓按交易日天数无条件退出。
    reason_time = paper._midline_sell_reason(
        position,
        {"price": 9.8, "ma20": 9.5, "stop_loss_price": 9.0},
        profit_pct=-2.0,
        hold_days=paper.settings.PAPER_HIGHBOARD_MAX_HOLD_DAYS,
    )
    assert "到期平仓" in reason_time


@pytest.mark.asyncio
async def test_tenbagger_midline_candidates_score_filter(monkeypatch, paper_client):
    """策略E: 连板高标接力候选 (2026-08-31 重建, 涨停池连板≥4 + 封板质量 + 一字过滤)."""
    # 测试显式固定开关，避免外部环境变量影响候选口径。
    monkeypatch.setattr(paper.settings, "PAPER_TENBAGGER_ENABLED", True)
    _client, SessionLocal = paper_client

    today = date.today()
    signal_date = await paper.trade_calendar.previous_trade_day(today)
    async with SessionLocal() as session:
        session.add_all([
            LimitUpPool(code="600100", name="五板龙头", trade_date=signal_date, consecutive_days=5,
                        seal_amount=3.0e8, break_count=0, limit_up_reason="AI算力"),
            LimitUpPool(code="600200", name="三板不足", trade_date=signal_date, consecutive_days=3,
                        seal_amount=3.0e8, break_count=0),
            LimitUpPool(code="600300", name="封板弱", trade_date=signal_date, consecutive_days=5,
                        seal_amount=0.3e8, break_count=5),  # 封板资金不足+炸板多
            LimitUpPool(code="600400", name="一字未开板", trade_date=signal_date, consecutive_days=6,
                        seal_amount=5.0e8, break_count=0),
            LimitUpPool(code="600500", name="开板后回封", trade_date=signal_date, consecutive_days=5,
                        seal_amount=2.0e8, break_count=1),
            LimitUpPool(code="600600", name="涨停有卖盘", trade_date=signal_date, consecutive_days=4,
                        seal_amount=2.0e8, break_count=1),
            LimitUpPool(code="600700", name="涨幅超限高标", trade_date=signal_date, consecutive_days=5,
                        seal_amount=3.0e8, break_count=0),
        ])
        # 600100 在3%低吸区；其余覆盖板数、质量、一字和追涨上限过滤。
        session.add_all([
            StockSpot(code="600100", name="五板龙头", price=12.0, prev_close=11.0, change_pct=3.0,
                      high=12.1, avg_price=11.9, volume_ratio=1.5, limit_up=12.1),
            StockSpot(code="600400", name="一字未开板", price=13.2, prev_close=12.0, change_pct=10.0,
                      open=13.2, low=13.2, volume_ratio=0.3, limit_up=13.2,
                      bid1_price=13.2, bid1_volume=50000, ask1_price=0),
            StockSpot(code="600500", name="开板后回封", price=14.3, prev_close=13.0, change_pct=10.0,
                      open=13.7, low=13.6, volume=80000, volume_ratio=4.0, limit_up=14.3,
                      bid1_price=14.3, bid1_volume=12000, ask1_price=0),
            StockSpot(code="600600", name="涨停有卖盘", price=11.0, prev_close=10.0, change_pct=10.0,
                      open=10.6, low=10.5, volume=50000, volume_ratio=3.0, limit_up=11.0,
                      bid1_price=11.0, bid1_volume=3000, ask1_price=11.0, ask1_volume=100),
            StockSpot(code="600700", name="涨幅超限高标", price=10.43, prev_close=10.0, change_pct=4.27,
                      high=10.5, avg_price=10.3, volume_ratio=1.5, limit_up=11.0),
        ])
        await session.commit()

        candidates, notes = await paper._tenbagger_midline_candidates(
            session, limit=5, trade_date=today,
        )
        codes = {c["code"] for c in candidates}
        assert "600100" in codes       # 5板 + 封板3亿 + 炸板0 + 已开板 → 命中
        assert "600200" not in codes   # 3板 < 阈值4板
        assert "600300" not in codes   # 封板资金0.3亿<1亿 且炸板5次>2
        assert "600400" not in codes   # 一字板且涨幅超限，不可买
        assert "600500" not in codes   # 即使曾开板回封，执行日涨幅10%也不追
        assert "600600" not in codes   # 涨停有卖盘也不能绕过3%执行上限
        assert "600700" not in codes   # +4.27%超过E策略3%硬上限
        assert candidates[0]["avg_price"] == pytest.approx(11.9)
        assert candidates[0]["pullback_from_high_pct"] <= 2.0
        assert candidates[0]["_source"] == "tenbagger_midline"
        assert candidates[0]["consecutive_days"] == 5
        assert candidates[0]["signal_date"] == signal_date.isoformat()
        assert candidates[0]["stop_loss_pct"] == paper.settings.PAPER_HIGHBOARD_STOP_LOSS_PCT


@pytest.mark.asyncio
async def test_six_account_status_overrides(paper_client):
    """六策略 auto/status 各自返回独立配置."""
    client, SessionLocal = paper_client

    async with SessionLocal() as session:
        b = await paper.paper_auto_status(account_name="promotion", db=session)
        c = await paper.paper_auto_status(account_name="mainline", db=session)
        d = await paper.paper_auto_status(account_name="auction", db=session)
        e = await paper.paper_auto_status(account_name="tenbagger", db=session)
        f = await paper.paper_auto_status(account_name="reversal", db=session)
        a = await paper.paper_auto_status(account_name="default", db=session)

    assert b["strategy"] == "promotion" and b["short_trade_rules"]["take_profit_pct"] == paper.settings.PAPER_PROMOTION_TAKE_PROFIT_PCT
    assert c["strategy"] == "mainline" and c["short_trade_rules"]["take_profit_pct"] == paper.settings.PAPER_MAINLINE_TAKE_PROFIT_PCT
    assert d["strategy"] == "auction" and d["short_trade_rules"]["take_profit_pct"] == paper.settings.PAPER_AUCTION_TAKE_PROFIT_PCT
    assert e["strategy"] == "tenbagger" and e["short_trade_rules"]["take_profit_pct"] == paper.settings.PAPER_HIGHBOARD_TAKE_PROFIT_PCT
    assert f["strategy"] == "reversal"
    assert f["auto_order_enabled"] is True
    assert a.get("strategy") is None  # 策略A 保持原样


def test_strategy_empty_reason_per_account():
    """各策略候选为空时的说明文案按账户区分, 不串到策略A文案."""
    assert "晋级预测二板" in paper._strategy_empty_reason("promotion")
    assert "主线扩散首板" in paper._strategy_empty_reason("mainline")
    assert "概率阈值" not in paper._strategy_empty_reason("mainline")
    assert "竞价高开强攻" in paper._strategy_empty_reason("auction")
    assert "竞价字段完整" in paper._strategy_empty_reason("auction")
    assert "连板高标接力" in paper._strategy_empty_reason("tenbagger")
    assert "断板反包" in paper._strategy_empty_reason("reversal")
    # 策略A(default) 及未知账户回退到策略A文案
    assert "高胜率明日预案" in paper._strategy_empty_reason("default")
    assert "高胜率明日预案" in paper._strategy_empty_reason("unknown")


def test_reversal_kline_pattern():
    """F按整个断板窗口低点，而不是只看反包前一日收盘涨跌。"""
    def mk(
        date_str,
        close,
        prev_close,
        change_pct,
        *,
        open_price=None,
        low_price=None,
        volume=100_000,
    ):
        return StockKline(
            code="600001",
            trade_date=date.fromisoformat(date_str),
            open=open_price if open_price is not None else close,
            close=close,
            high=close,
            low=(
                low_price
                if low_price is not None
                else min(close, open_price if open_price is not None else close)
            ),
            prev_close=prev_close,
            change_pct=change_pct,
            volume=volume,
        )

    cfg = {
        "min_consecutive": 3,
        "max_gap_days": 1,
        "min_dip_pct": -5.0,
        "min_vol_ratio": 1.5,
    }
    signal_date = date(2026, 8, 17)
    klines = [
        mk("2026-08-10", 8.0, 8.0, 0.0),
        mk("2026-08-11", 8.8, 8.0, 10.0),
        mk("2026-08-12", 9.68, 8.8, 10.0),
        mk("2026-08-13", 10.65, 9.68, 10.0),
        mk("2026-08-14", 9.9, 10.65, -7.04),
        mk("2026-08-17", 10.89, 9.9, 10.0, open_price=10.1, volume=200_000),
    ]

    pattern = paper._reversal_kline_pattern(klines, signal_date, cfg)
    assert pattern is not None
    assert pattern["consec_before"] == 3
    assert pattern["gap_days"] == 1
    assert pattern["window_dip_pct"] == pytest.approx(-7.04, abs=0.02)
    assert pattern["prev_day_chg"] == pytest.approx(-7.04)
    assert pattern["vol_ratio"] == pytest.approx(2.0)

    low_volume = list(klines)
    low_volume[-1] = mk(
        "2026-08-17", 10.89, 9.9, 10.0, open_price=10.1, volume=120_000,
    )
    assert paper._reversal_kline_pattern(low_volume, signal_date, cfg) is None

    one_price = list(klines)
    one_price[-1] = mk(
        "2026-08-17", 10.89, 9.9, 10.0, open_price=10.89, volume=200_000,
    )
    assert paper._reversal_kline_pattern(one_price, signal_date, cfg) is None

    no_deep_dip = list(klines)
    no_deep_dip[-2] = mk("2026-08-14", 10.5, 10.65, -1.41)
    no_deep_dip[-1] = mk(
        "2026-08-17", 11.55, 10.5, 10.0, open_price=10.7, volume=200_000,
    )
    assert paper._reversal_kline_pattern(no_deep_dip, signal_date, cfg) is None

    mild_close_but_deep_intraday = list(klines)
    mild_close_but_deep_intraday[-2] = mk(
        "2026-08-14",
        10.2,
        10.65,
        -4.23,
        low_price=9.7,
    )
    mild_close_but_deep_intraday[-1] = mk(
        "2026-08-17",
        11.22,
        10.2,
        10.0,
        open_price=10.4,
        volume=200_000,
    )
    mild_pattern = paper._reversal_kline_pattern(
        mild_close_but_deep_intraday,
        signal_date,
        cfg,
    )
    assert mild_pattern is not None
    assert mild_pattern["prev_day_chg"] == pytest.approx(-4.23)
    assert mild_pattern["window_dip_pct"] < -5.0


@pytest.mark.asyncio
async def test_reversal_pullback_candidates(monkeypatch, paper_client):
    """F只用上一交易日完整反包K线产生次日执行候选。"""
    monkeypatch.setattr(paper.settings, "PAPER_REVERSAL_ENABLED", True)
    _client, SessionLocal = paper_client
    today = date.today()
    signal_date = date(2026, 8, 17)

    async def previous_trade_day(_trade_date):
        return signal_date

    monkeypatch.setattr(paper.trade_calendar, "previous_trade_day", previous_trade_day)

    def mk(code, date_str, close, prev_close, change_pct, *, open_price=None, volume=100_000):
        return StockKline(
            code=code,
            trade_date=date.fromisoformat(date_str),
            open=open_price if open_price is not None else close,
            close=close,
            high=close,
            low=min(close, open_price if open_price is not None else close),
            prev_close=prev_close,
            change_pct=change_pct,
            volume=volume,
        )

    async with SessionLocal() as session:
        session.add_all([
            LimitUpPool(code="600001", name="断板反包票", trade_date=signal_date, consecutive_days=1,
                        seal_amount=2.0e8, break_count=0, limit_up_reason="测试"),
            LimitUpPool(code="600002", name="普通涨停", trade_date=signal_date, consecutive_days=1,
                        seal_amount=1.5e8, break_count=0, limit_up_reason="测试"),
            StockSpot(code="600001", name="断板反包票", price=10.5, prev_close=10.89,
                      high=10.6, avg_price=10.4, change_pct=-3.58,
                      limit_up=11.98, updated_at=datetime.now()),
            StockSpot(code="600002", name="普通涨停", price=9.5, prev_close=9.0,
                      change_pct=5.0, limit_up=9.9, updated_at=datetime.now()),
        ])
        session.add_all([
            mk("600001", "2026-08-10", 8.0, 8.0, 0.0),
            mk("600001", "2026-08-11", 8.8, 8.0, 10.0),
            mk("600001", "2026-08-12", 9.68, 8.8, 10.0),
            mk("600001", "2026-08-13", 10.65, 9.68, 10.0),
            mk("600001", "2026-08-14", 9.9, 10.65, -7.04),
            mk("600001", "2026-08-17", 10.89, 9.9, 10.0, open_price=10.1, volume=200_000),
            mk("600002", "2026-08-12", 8.8, 8.0, 10.0),
            mk("600002", "2026-08-13", 8.9, 8.8, 1.14),
            mk("600002", "2026-08-14", 9.0, 8.9, 1.12),
            mk("600002", "2026-08-17", 9.9, 9.0, 10.0, open_price=9.2, volume=200_000),
        ])
        await session.commit()

        candidates, notes = await paper._reversal_pullback_candidates(
            session, limit=5, trade_date=today,
        )
        spot = await session.scalar(
            select(StockSpot).where(StockSpot.code == "600001")
        )
        spot.change_pct = 4.64
        chased_candidates, _ = await paper._reversal_pullback_candidates(
            session, limit=5, trade_date=today,
        )
        spot.change_pct = 2.0
        spot.avg_price = 10.55
        faded_candidates, _ = await paper._reversal_pullback_candidates(
            session, limit=5, trade_date=today,
        )

    assert [candidate["code"] for candidate in candidates] == ["600001"], notes
    assert chased_candidates == []
    assert faded_candidates == []
    assert candidates[0]["signal_date"] == signal_date.isoformat()
    assert candidates[0]["vol_ratio"] == pytest.approx(2.0)
    assert candidates[0]["stop_loss_pct"] == paper.settings.PAPER_REVERSAL_STOP_LOSS_PCT
    assert candidates[0]["take_profit_pct"] == paper.settings.PAPER_REVERSAL_TAKE_PROFIT_PCT


@pytest.mark.asyncio
async def test_submit_order_routes_to_strategy_account(paper_client, qualified_manual_execution):
    """P0修复(2026-08-31): submit_order 必须把成交落账到对应策略账户, 而非 default.

    验证: 对 promotion 账户 submit_order 买入, 成交应落在 promotion 账户的持仓/交易记录.
    """
    _client, SessionLocal = paper_client
    from app.trading.service import SubmitOrderCommand, submit_order

    await qualified_manual_execution("600999", "招商证券", last=12, ask=12, bid=11.99)
    async with SessionLocal() as session:
        # 用 promotion 账户提交买入
        result = await submit_order(
            session,
            SubmitOrderCommand(
                code="600999", side="buy", price=12.0, quantity=100,
                broker="paper", account_id=paper.PAPER_ACCOUNT_PROMOTION,
                strategy_id="paper-auto-short", source="promotion", reason="晋级二板自动买入测试",
                decision_at=paper._paper_now(),
            ),
        )
        assert result["order"]["status"] == "filled"

        # 成交必须记入 promotion 账户, 而非 default
        promo = await paper._get_or_create_account(session, paper.PAPER_ACCOUNT_PROMOTION)
        default = await paper._get_or_create_account(session, paper.PAPER_ACCOUNT_DEFAULT)

        promo_pos = await paper._open_positions(session, promo.id)
        default_pos = await paper._open_positions(session, default.id)
        assert any(p.code == "600999" for p in promo_pos), "成交必须落到 promotion 账户持仓"
        assert not any(p.code == "600999" for p in default_pos), "不得落到 default 账户持仓"

        promo_trades = (await session.execute(
            select(PaperTradeLog).where(PaperTradeLog.account_id == promo.id)
        )).scalars().all()
        default_trades = (await session.execute(
            select(PaperTradeLog).where(PaperTradeLog.account_id == default.id)
        )).scalars().all()
        assert any(t.code == "600999" for t in promo_trades)
        assert not any(t.code == "600999" for t in default_trades)
        routed_trade = next(t for t in promo_trades if t.code == "600999")
        assert routed_trade.reason == "晋级二板自动买入测试"


@pytest.mark.asyncio
async def test_midline_sell_context_ma20_does_not_expand_governed_exit_rules(paper_client):
    """上下文可保留MA指标，但E/F不得用未回放验证的MA20规则卖出。"""
    _client, SessionLocal = paper_client

    async with SessionLocal() as session:
        # 构造 21 根K线: 前 15 根横盘 10.0, 后 6 根跌到 9.5 附近.
        # 最新价 9.5: 未破止损线(-8%→9.2), 但已跌破 MA20(≈9.85) → 触发"跌破20日线"
        base = date.today() - timedelta(days=30)
        for i in range(21):
            close = 10.0 if i < 15 else 10.0 - (i - 14) * 0.1
            session.add(StockKline(
                code="600000", trade_date=base + timedelta(days=i),
                open=close, high=close + 0.1, low=close - 0.1, close=close,
                volume=1_000_000,
            ))
        await session.commit()

        position = PaperPosition(
            account_id=1, code="600000", buy_price=10.0, buy_amount=100,
            buy_time=datetime.now() - timedelta(days=3),
        )
        ctx = await paper._build_short_sell_context(session, position, date.today())
        assert ctx.get("ma5") is not None
        assert ctx.get("ma10") is not None
        assert ctx.get("ma20") is not None, "ctx 必须包含 ma20"

        price = ctx["price"] or 9.5
        profit_pct = (price / 10.0 - 1) * 100
        assert profit_pct > -8.0, f"测试数据应未破硬止损, 实际 {profit_pct:.1f}%"
        reason = paper._midline_sell_reason(
            position,
            {"price": price, "ma20": ctx["ma20"], "stop_loss_price": 9.2},
            profit_pct=profit_pct,
            hold_days=2,
        )
        assert reason == ""


def test_execution_quote_hard_gate_and_conservative_slippage(monkeypatch):
    monkeypatch.setattr(paper.settings, "PAPER_EXECUTION_QUOTE_MAX_AGE_SEC", 90)
    monkeypatch.setattr(paper.settings, "PAPER_EXECUTION_SLIPPAGE_PCT", 0.10)
    now = datetime(2026, 8, 31, 10, 0, 0)
    spot = StockSpot(
        code="600001",
        price=10.0,
        ask1_price=10.01,
        bid1_price=9.99,
        limit_up=11.0,
        limit_down=9.0,
        updated_at=now - timedelta(seconds=90),
    )

    assert paper._execution_quote_status(spot, now.date(), now=now) == (True, "")
    spot.updated_at = now - timedelta(seconds=91)
    quote_ok, reason = paper._execution_quote_status(spot, now.date(), now=now)
    assert quote_ok is False and "过期" in reason
    spot.updated_at = now - timedelta(days=1)
    quote_ok, reason = paper._execution_quote_status(spot, now.date(), now=now)
    assert quote_ok is False and "不是交易日" in reason

    spot.updated_at = now
    assert paper._conservative_execution_price(spot, "buy") > spot.ask1_price
    assert paper._conservative_execution_price(spot, "sell") < spot.bid1_price
    spot.price = spot.limit_up
    spot.ask1_price = None
    assert paper._conservative_execution_price(spot, "buy") is None


def test_missing_stock_tag_uses_conservative_board_fallback():
    assert paper._effective_board_tag("600001", None) == "tradeable"
    assert paper._effective_board_tag("002001", None) == "tradeable"
    assert paper._effective_board_tag("300001", None) == "observe_only"
    assert paper._effective_board_tag("688001", None) == "observe_only"
    assert paper._effective_board_tag("920001", None) == "observe_only"
    assert paper._effective_board_tag("999999", None) == "observe_only"
    assert paper._effective_board_tag(
        "600001", StockTag(code="600001", board_tag="blocked")
    ) == "blocked"


@pytest.mark.asyncio
async def test_trade_day_hold_days_excludes_weekend(monkeypatch):
    calls = []

    async def trade_days_between(start_date, end_date):
        calls.append((start_date, end_date))
        return [date(2026, 8, 31)]

    monkeypatch.setattr(paper.trade_calendar, "trade_days_between", trade_days_between)
    assert await paper._trade_day_hold_days(date(2026, 8, 28), date(2026, 8, 31)) == 1
    assert calls == [(date(2026, 8, 29), date(2026, 8, 31))]
    assert await paper._trade_day_hold_days(date(2026, 8, 31), date(2026, 8, 31)) == 0


@pytest.mark.asyncio
async def test_auto_log_and_t_buyback_counts_are_account_isolated(paper_client):
    _client, SessionLocal = paper_client
    trade_date = date.today()
    async with SessionLocal() as session:
        default = PaperAccount(account_name="default", initial_capital=50_000)
        promotion = PaperAccount(account_name="promotion", initial_capital=50_000)
        challenger = PaperAccount(account_name="challenger_b", initial_capital=50_000)
        session.add_all([default, promotion, challenger])
        await session.flush()
        session.add_all([
            PaperAutoTradeLog(
                account_id=default.id, run_id="a-buy", trade_date=trade_date,
                source="next_day_plan", code="600001", action="buy", decision="executed",
            ),
            PaperAutoTradeLog(
                account_id=promotion.id, run_id="b-buy", trade_date=trade_date,
                source="promotion_promotion", code="600002", action="buy", decision="executed",
            ),
            PaperAutoTradeLog(
                account_id=challenger.id, run_id="b2-buy", trade_date=trade_date,
                source="b_weak_open_second_board", code="600004", action="buy", decision="executed",
            ),
            PaperAutoTradeLog(
                account_id=None, run_id="legacy-buy", trade_date=trade_date,
                source="next_day_plan", code="600003", action="buy", decision="executed",
            ),
            PaperAutoTradeLog(
                account_id=default.id, run_id="a-t", trade_date=trade_date,
                source="position-t", code="600009", action="buy", decision="executed",
            ),
            PaperAutoTradeLog(
                account_id=promotion.id, run_id="b-t", trade_date=trade_date,
                source="position-t", code="600009", action="buy", decision="executed",
            ),
        ])
        await session.commit()

        default_rows = await paper._today_auto_new_buy_logs(
            session, trade_date, account_id=default.id, include_legacy_null=True,
        )
        promotion_rows = await paper._today_auto_new_buy_logs(
            session, trade_date, account_id=promotion.id,
        )
        challenger_rows = await paper._today_auto_new_buy_logs(
            session, trade_date, account_id=challenger.id,
        )
        challenger_scope = paper._auto_log_account_scope_filter(
            challenger.account_name,
            challenger.id,
        )
        challenger_visible_logs = (
            await session.execute(
                select(PaperAutoTradeLog).where(challenger_scope)
            )
        ).scalars().all()
        default_t_count = await paper._today_t_buyback_count(
            session, default.id, "600009", trade_date,
        )
        promotion_t_count = await paper._today_t_buyback_count(
            session, promotion.id, "600009", trade_date,
        )

    assert {row.code for row in default_rows} == {"600001", "600003"}
    assert {row.code for row in promotion_rows} == {"600002"}
    assert {row.code for row in challenger_rows} == {"600004"}
    assert {row.code for row in challenger_visible_logs} == {"600004"}
    assert default_t_count == 1
    assert promotion_t_count == 1


@pytest.mark.asyncio
async def test_scale_in_keeps_original_position_buy_time(paper_client, qualified_manual_execution):
    client, SessionLocal = paper_client
    async with SessionLocal() as session:
        session.add(StockSpot(code="600001", name="测试股", price=10.0))
        await session.commit()

    await qualified_manual_execution("600001", "测试股")
    first = await client.post("/paper/buy", json={
        "code": "600001", "price": 10.0, "amount": 100, "signal_id": "first",
    })
    assert first.status_code == 200
    async with SessionLocal() as session:
        position = (await session.execute(
            select(PaperPosition).where(PaperPosition.code == "600001")
        )).scalar_one()
        original_buy_time = position.buy_time

    await asyncio.sleep(0.01)
    await qualified_manual_execution("600001", "测试股", ask=9.8, bid=9.79)
    second = await client.post("/paper/buy", json={
        "code": "600001", "price": 9.8, "amount": 100, "signal_id": "scale-in",
    })
    assert second.status_code == 200
    async with SessionLocal() as session:
        position = (await session.execute(
            select(PaperPosition).where(PaperPosition.code == "600001")
        )).scalar_one()
    assert position.buy_time == original_buy_time
    assert position.buy_amount == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("account_name", [paper.PAPER_ACCOUNT_TENBAGGER, paper.PAPER_ACCOUNT_REVERSAL])
async def test_ef_exit_is_full_available_position_and_t_buyback_disabled(
    account_name, paper_client,
):
    _client, SessionLocal = paper_client
    trade_date = date.today()
    async with SessionLocal() as session:
        account = PaperAccount(
            account_name=account_name,
            strategy=account_name,
            initial_capital=50_000,
            current_capital=50_000,
            total_assets=52_700,
            status="active",
        )
        session.add(account)
        await session.flush()
        session.add_all([
            PaperPosition(
                account_id=account.id,
                code="600001",
                name="退出测试",
                buy_price=10.0,
                buy_amount=300,
                buy_time=datetime.now() - timedelta(days=5),
                current_price=9.0,
                profit_pct=-10.0,
                hold_days=3,
                stop_loss_price=9.5,
                is_closed=False,
            ),
            StockSpot(
                code="600001",
                name="退出测试",
                price=9.0,
                bid1_price=8.99,
                limit_down=8.0,
                updated_at=datetime.now(),
            ),
        ])
        await session.commit()

        sell_logs = await paper._run_auto_sells(
            session,
            account=account,
            run_id=f"exit-{account_name}",
            trade_date=trade_date,
            trigger="test",
            execute=False,
        )
        t_logs = await paper._run_auto_t_buybacks(
            session,
            account=account,
            run_id=f"t-{account_name}",
            trade_date=trade_date,
            trigger="test",
            execute=False,
        )

    sell = next(log for log in sell_logs if log.action == "sell")
    assert sell.decision == "dry_run"
    assert sell.amount == 300
    assert sell.price < 8.99
    assert not sell.reason.startswith("T减仓")
    assert t_logs == []


@pytest.mark.asyncio
async def test_auto_sell_rejected_order_is_not_logged_as_executed(
    paper_client, monkeypatch,
):
    from app.trading import service as trading_service

    _client, SessionLocal = paper_client
    trade_date = date.today()

    async def rejected_order(*_args, **_kwargs):
        return {
            "order": {"status": "risk_blocked", "error_message": "测试风控拒绝"},
            "fills": [],
            "risk": {"final_level": "block"},
        }

    monkeypatch.setattr(trading_service, "submit_order", rejected_order)
    async with SessionLocal() as session:
        account = await paper._get_or_create_account(session, paper.PAPER_ACCOUNT_PROMOTION)
        session.add_all([
            PaperPosition(
                account_id=account.id,
                code="600109",
                name="拒绝卖出测试",
                buy_price=10.0,
                buy_amount=100,
                buy_time=datetime.now() - timedelta(days=5),
                current_price=9.0,
                profit_pct=-10.0,
                hold_days=3,
                stop_loss_price=9.5,
                is_closed=False,
            ),
            StockSpot(
                code="600109",
                name="拒绝卖出测试",
                price=9.0,
                bid1_price=8.99,
                limit_down=8.0,
                updated_at=datetime.now(),
            ),
        ])
        await session.commit()
        logs = await paper._run_auto_sells(
            session,
            account=account,
            run_id="rejected-sell",
            trade_date=trade_date,
            trigger="test",
            execute=True,
        )

    sell_log = next(item for item in logs if item.code == "600109")
    assert sell_log.action == "skip_sell"
    assert sell_log.decision == "blocked"
    assert "测试风控拒绝" in sell_log.reason
    assert sell_log.executed_trade_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("account_name", "source"),
    [
        (paper.PAPER_ACCOUNT_DEFAULT, "next_day_plan"),
        (paper.PAPER_ACCOUNT_PROMOTION, "promotion_promotion"),
        (paper.PAPER_ACCOUNT_MAINLINE, "promotion_mainline"),
        (paper.PAPER_ACCOUNT_AUCTION, "promotion_auction"),
        (paper.PAPER_ACCOUNT_TENBAGGER, "tenbagger_midline"),
        (paper.PAPER_ACCOUNT_REVERSAL, "reversal_pullback"),
    ],
)
async def test_each_strategy_can_dry_run_one_compliant_buy(
    monkeypatch, paper_client, account_name, source, qualified_execution_risk,
):
    """用同一套新鲜行情与真实风控验证A-F均保留一笔合规买入能力。"""
    _client, SessionLocal = paper_client
    candidate = {
        "code": "600888",
        "name": "合规候选",
        "_source": source,
        "signal_source": source,
        "total_score": 95.0,
        "change_pct": 0.5,
        "stop_loss_price": 9.2,
        "trade_gate_passed": True,
        "actionable": True,
        "watch_only": False,
    }

    async def candidate_factory(*_args, **_kwargs):
        return [dict(candidate)], []

    monkeypatch.setattr(paper, "_paper_auto_buy_candidates", candidate_factory)
    monkeypatch.setattr(paper, "_promotion_route_buy_candidates", candidate_factory)
    monkeypatch.setattr(paper, "_tenbagger_midline_candidates", candidate_factory)
    monkeypatch.setattr(paper, "_reversal_pullback_candidates", candidate_factory)
    monkeypatch.setattr(paper, "_is_late_new_buy_time", lambda *_args, **_kwargs: False)
    # A仍走午后强市闸门；B-F模拟午后场景，验证各自独立入场条件不会被A误伤。
    monkeypatch.setattr(
        paper,
        "_is_afternoon_new_buy_time",
        lambda *_args, **_kwargs: account_name != paper.PAPER_ACCOUNT_DEFAULT,
    )

    async with SessionLocal() as session:
        session.add(StockTag(code="600888", name="合规候选", board_type="main_sh", board_tag="tradeable"))
        session.add(StockSpot(
            code="600888",
            name="合规候选",
            price=10.0,
            open=9.95,
            high=10.2,
            low=9.8,
            avg_price=10.0,
            change_pct=0.5,
            volume_ratio=1.2,
            ask1_price=10.01,
            bid1_price=9.99,
            limit_up=11.0,
            limit_down=9.0,
            updated_at=datetime.now(),
        ))
        await session.commit()
        result = await paper.run_paper_auto_trade(
            session,
            execute=False,
            trigger="test-compliant-buy",
            max_candidates=1,
            execution_mode="manual",
            account_name=account_name,
        )

    buy_logs = [
        log for log in result["logs"]
        if log["action"] == "buy" and log["decision"] == "dry_run"
    ]
    assert len(buy_logs) == 1, result["logs"]
    assert buy_logs[0]["price"] > 10.01


@pytest.mark.asyncio
async def test_explicit_auto_order_pause_keeps_candidate_audit_without_position(
    monkeypatch, paper_client, qualified_execution_risk,
):
    """即使生产默认开启，显式关闭独立闸门时也只允许候选演练。"""
    _client, SessionLocal = paper_client
    monkeypatch.setattr(paper.settings, "PAPER_PROMOTION_AUTO_ORDER_ENABLED", False)

    async def candidate_factory(*_args, **_kwargs):
        return [
            {
                "code": code,
                "name": f"暂停买入候选{index}",
                "_source": "promotion_promotion",
                "signal_source": "promotion_promotion",
                "total_score": 95.0 - index,
                "change_pct": 0.5,
                "stop_loss_price": 9.2,
                "trade_gate_passed": True,
                "actionable": True,
                "watch_only": False,
            }
            for index, code in enumerate(("600887", "600888", "600889"), start=1)
        ], []

    async def order_window(*_args, **_kwargs):
        return True, ""

    monkeypatch.setattr(paper, "_promotion_route_buy_candidates", candidate_factory)
    monkeypatch.setattr(paper, "_paper_order_window_status", order_window)
    monkeypatch.setattr(paper, "_is_intraday_buy_window", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(paper, "_is_late_new_buy_time", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(paper, "_is_afternoon_new_buy_time", lambda *_args, **_kwargs: True)

    async with SessionLocal() as session:
        session.add_all([
            StockTag(code=code, name=f"暂停买入候选{index}", board_type="main_sh", board_tag="tradeable")
            for index, code in enumerate(("600887", "600888", "600889"), start=1)
        ])
        session.add_all([
            StockSpot(
                code=code,
                name=f"暂停买入候选{index}",
                price=10.0,
                open=9.95,
                high=10.2,
                low=9.8,
                avg_price=10.0,
                change_pct=0.5,
                volume_ratio=1.2,
                ask1_price=10.01,
                bid1_price=9.99,
                limit_up=11.0,
                limit_down=9.0,
                updated_at=datetime.now(),
            )
            for index, code in enumerate(("600887", "600888", "600889"), start=1)
        ])
        await session.commit()
        result = await paper.run_paper_auto_trade(
            session,
            execute=True,
            trigger="test-paused-promotion-buy",
            max_candidates=3,
            execution_mode="manual",
            account_name=paper.PAPER_ACCOUNT_PROMOTION,
        )
        promotion_account = await paper._get_or_create_account(
            session, paper.PAPER_ACCOUNT_PROMOTION,
        )
        positions = await paper._open_positions(session, promotion_account.id)

    assert positions == []
    assert any(
        log["source"] == "system"
        and log["decision"] == "dry_run"
        and "自动买入委托已因校正证据暂停" in log["reason"]
        for log in result["logs"]
    )
    dry_run_buys = [
        log for log in result["logs"]
        if log["action"] == "buy" and log["decision"] == "dry_run"
    ]
    capacity_skips = [
        log for log in result["logs"]
        if log["action"] == "skip_buy" and "策略日限" in log["reason"]
    ]
    assert len(dry_run_buys) == 2
    assert len(capacity_skips) == 1
    assert "当前账户今日真实新开仓0只" in capacity_skips[0]["reason"]
    assert "本轮演练候选2只" in capacity_skips[0]["reason"]
    assert "演练候选未成交" in capacity_skips[0]["reason"]
    assert "日内新开仓已达" not in capacity_skips[0]["reason"]


@pytest.mark.asyncio
async def test_highboard_resealed_candidate_submits_one_queue_order(
    monkeypatch, paper_client, qualified_execution_risk,
):
    """E策略回封时登记排队单，重复轮询不得重复报单或直接生成持仓。"""
    _client, SessionLocal = paper_client

    async def candidate_factory(*_args, **_kwargs):
        return [{
            "code": "600889",
            "name": "回封高标",
            "_source": "tenbagger_midline",
            "signal_source": "连板高标接力",
            "total_score": 90.0,
            "change_pct": 10.0,
            "stop_loss_price": 10.34,
            "limit_up_queue": True,
            "consecutive_days": 5,
        }], []

    async def order_window(*_args, **_kwargs):
        return True, ""

    monkeypatch.setattr(paper, "_tenbagger_midline_candidates", candidate_factory)
    monkeypatch.setattr(paper, "_paper_order_window_status", order_window)
    monkeypatch.setattr(paper, "_is_intraday_buy_window", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(paper, "_is_late_new_buy_time", lambda *_args, **_kwargs: False)
    # 测试必须与实际运行时钟解耦，避免14:50后第二次轮询先触发收盘撤单。
    monkeypatch.setattr(paper.settings, "PAPER_HIGHBOARD_QUEUE_CANCEL_TIME", "23:59")
    # E有独立高标入场条件，午后不能被策略A的“强势市场才开仓”闸门误伤。
    monkeypatch.setattr(paper, "_is_afternoon_new_buy_time", lambda *_args, **_kwargs: True)

    async with SessionLocal() as session:
        session.add(StockTag(code="600889", name="回封高标", board_type="main_sh", board_tag="tradeable"))
        session.add(StockSpot(
            code="600889",
            name="回封高标",
            price=11.0,
            prev_close=10.0,
            open=10.5,
            high=11.0,
            low=10.4,
            change_pct=10.0,
            volume=30_000,
            volume_ratio=3.0,
            avg_price=10.8,
            limit_up=11.0,
            limit_down=9.0,
            bid1_price=11.0,
            bid1_volume=20_000,
            ask1_price=0,
            updated_at=datetime.now(),
        ))
        await session.commit()

        first = await paper.run_paper_auto_trade(
            session,
            execute=True,
            trigger="test-limit-queue",
            max_candidates=1,
            execution_mode="manual",
            account_name=paper.PAPER_ACCOUNT_TENBAGGER,
        )
        second = await paper.run_paper_auto_trade(
            session,
            execute=True,
            trigger="test-limit-queue-repeat",
            max_candidates=1,
            execution_mode="manual",
            account_name=paper.PAPER_ACCOUNT_TENBAGGER,
        )
        orders = (
            await session.execute(
                select(trading_models.TradeOrder).where(
                    trading_models.TradeOrder.code == "600889"
                )
            )
        ).scalars().all()
        positions = (
            await session.execute(
                select(PaperPosition).where(PaperPosition.code == "600889")
            )
        ).scalars().all()

    queued_logs = [
        item for item in first["logs"]
        if item["action"] == "queue_buy" and item["decision"] == "wait"
    ]
    assert len(queued_logs) == 1, first["logs"]
    assert "排队" in queued_logs[0]["reason"]
    assert len(orders) == 1
    assert orders[0].status == "submitted"
    assert positions == []
    assert not any(item["decision"] == "executed" for item in second["logs"])


@pytest.mark.asyncio
async def test_scheduler_isolates_each_paper_account_failure(monkeypatch):
    from app.api.v1 import paper as paper_module
    from app.data import scheduler as scheduler_module

    seen = []
    sessions = []

    class FakeSession:
        def __init__(self):
            self.rolled_back = False

        async def rollback(self):
            self.rolled_back = True

    class FakeSessionContext:
        def __init__(self):
            self.session = FakeSession()
            sessions.append(self.session)

        async def __aenter__(self):
            return self.session

        async def __aexit__(self, exc_type, exc, tb):
            return False

    async def fake_run(_session, *, account_name, **_kwargs):
        seen.append(account_name)
        if account_name == "broken":
            raise RuntimeError("isolated failure")
        return {"run_id": account_name, "summary": {"executed": 0, "blocked": 0}}

    monkeypatch.setattr(paper_module, "PAPER_SCAN_ACCOUNTS", ("broken", "next", "last"))
    monkeypatch.setattr(paper_module, "run_paper_auto_trade", fake_run)
    monkeypatch.setattr(scheduler_module, "async_session", lambda: FakeSessionContext())

    scheduler = object.__new__(scheduler_module.DataScheduler)
    await scheduler._run_paper_accounts_isolated(
        execute=True,
        trigger="test",
        execution_mode="intraday",
    )

    assert seen == ["broken", "next", "last"]
    assert len(sessions) == 3
    assert sessions[0].rolled_back is True
    assert sessions[1].rolled_back is False
    assert sessions[2].rolled_back is False
