from datetime import date, datetime, time, timedelta

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.v1 import paper, trading
from app.db.session import Base, get_db
from app.models import paper as paper_models  # noqa: F401
from app.models import stock as stock_models  # noqa: F401
from app.models import trading as trading_models  # noqa: F401
from app.models.stock import MarketSentiment, StockSpot, StockTag
from app.risk.engine import risk_engine
from app.risk.rules import register_all_rules
from app.trading import service as trading_service


@pytest_asyncio.fixture
async def trading_client(tmp_path, monkeypatch):
    monkeypatch.setattr(trading_service.settings, "PAPER_CONTINUOUS_EXPERIMENT_ENABLED", False)
    db_path = tmp_path / "trading.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", future=True)
    SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with SessionLocal() as session:
        # 成交/排队测试显式提供独立身份投影；只有spot不再等于可以买入。
        for code, name in {
            "000001": "平安银行", "000003": "恢复测试", "000004": "情绪缺失测试",
            "600500": "回封排队样本", "600503": "版本切换排队",
            "600501": "排队撤单样本", "600502": "手动撤排队",
        }.items():
            session.add(StockTag(
                code=code, name=name,
                board_type="main_sh" if code.startswith("6") else "main_sz",
                board_tag="tradeable", is_st=False, is_delisting=False, is_suspended=False,
            ))
        session.add(
            MarketSentiment(
                trade_date=date.today(),
                sentiment_cycle="recovery",
                sentiment_score=60,
                limit_up_count=40,
                limit_down_count=5,
                broken_limit_count=8,
                seal_rate=70,
                board_height=3,
                advance_decline_ratio=1.2,
                turnover_total=1.2,
                main_net_inflow=20,
                quality_status="ok",
                quality_reason="",
                calculation_version="test_v1",
            )
        )
        await session.commit()

    # 该 fixture 会重建全局风控链；用例结束必须恢复，避免后续使用独立
    # 临时数据库的测试继承本文件状态并出现顺序相关的误拦截。
    previous_rules = list(risk_engine._rules)
    risk_engine._rules = []
    register_all_rules()

    app = FastAPI()
    app.include_router(trading.router, prefix="/trading")

    async def override_get_db():
        async with SessionLocal() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, SessionLocal

    app.dependency_overrides.clear()
    risk_engine._rules = previous_rules
    await engine.dispose()


@pytest.mark.asyncio
async def test_submit_order_creates_order_fill_and_position(trading_client, monkeypatch):
    from app.models.governance import TradeCalendarModel
    from app.models.stock import QuoteRound
    client, SessionLocal = trading_client
    at = datetime(2026, 9, 14, 10)
    monkeypatch.setattr(paper, "_public_order_clock", lambda: at)
    monkeypatch.setattr(paper, "_paper_now", lambda: at)
    async with SessionLocal() as session:
        sentiment = await session.scalar(select(MarketSentiment))
        sentiment.trade_date = at.date()
        session.add(TradeCalendarModel(trade_date=at.date(),is_trade_day=True,session_type="full"))
        session.add(QuoteRound(round_id="manual-test",source="tencent",trade_date=at.date(),
            committed_at=at-timedelta(seconds=1),as_of_at=at-timedelta(seconds=3),
            expected_count=1,received_count=1,source_time_count=1,coverage=1,source_time_coverage=1,
            quality_status="ok",config_version="fixture",code_version="fixture"))
        session.add(StockSpot(code="000001", name="平安银行", price=11.0,
            source_quote_at=at-timedelta(seconds=3), received_at=at-timedelta(seconds=2),
            updated_at=at-timedelta(seconds=1), quote_round_id="manual-test",
            limit_down=9,limit_up=12,ask1_price=10,ask1_volume=100,bid1_price=9.99,bid1_volume=100))
        await session.commit()

    response = await client.post("/trading/orders", json={
        "code": "000001",
        "side": "buy",
        "price": 10.0,
        "quantity": 100,
        "source": "test",
        "reason": "unit-test",
    })

    assert response.status_code == 200
    payload = response.json()
    assert payload["order"]["status"] == "filled"
    assert payload["order"]["filled_quantity"] == 100
    assert payload["fills"][0]["price"] == 10.0

    sync_response = await client.post("/trading/sync")
    assert sync_response.status_code == 200
    assert sync_response.json()["positions"][0]["code"] == "000001"


@pytest.mark.asyncio
async def test_submit_order_is_blocked_by_pre_trade_risk(trading_client):
    client, SessionLocal = trading_client
    async with SessionLocal() as session:
        session.add(StockSpot(code="000002", name="风险股", price=8.0))
        session.add(StockTag(
            code="000002",
            name="风险股",
            board_type="main_sz",
            board_tag="blocked",
            is_st=True,
        ))
        await session.commit()

    response = await client.post("/trading/orders", json={
        "code": "000002",
        "side": "buy",
        "price": 8.0,
        "quantity": 100,
    })

    assert response.status_code == 200
    payload = response.json()
    assert payload["order"]["status"] == "risk_blocked"
    assert payload["fills"] == []


@pytest.mark.asyncio
async def test_submit_order_fails_closed_without_today_sentiment(trading_client):
    client, SessionLocal = trading_client
    async with SessionLocal() as session:
        sentiment = await session.scalar(
            select(MarketSentiment).where(
                MarketSentiment.trade_date == date.today()
            )
        )
        await session.delete(sentiment)
        session.add(StockSpot(code="000004", name="情绪缺失测试", price=10.0))
        await session.commit()

    response = await client.post(
        "/trading/orders",
        json={
            "code": "000004",
            "side": "buy",
            "price": 10.0,
            "quantity": 100,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["order"]["status"] == "risk_blocked"
    assert any(
        item["rule"] == "sentiment_circuit_breaker"
        for item in payload["risk"]["block_reasons"]
    )


@pytest.mark.asyncio
async def test_only_internal_paper_recovery_order_can_cross_standard_drawdown_limit(trading_client, monkeypatch):
    # Seed an observed peak AND drawdown. Pre-open reporting must not invent a
    # new NAV merely to complete this fixture; the risk history is self-contained.
    client, SessionLocal = trading_client
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
        session.add(paper_models.PaperNav(
            account_id=account.id,
            trade_date=date(2026, 9, 9),
            nav=1.2,
            daily_return=0,
        ))
        session.add(paper_models.PaperNav(
            account_id=account.id,
            trade_date=date(2026, 9, 10),
            nav=1.0,
            daily_return=-16.666667,
        ))
        session.add(StockSpot(code="000003", name="恢复测试", price=10.0))
        await session.commit()
        from paper_immediate_fixture import seed_immediate_quote
        at=datetime(2026,9,14,10)
        sentiment=await session.scalar(select(MarketSentiment))
        sentiment.trade_date=at.date()
        await seed_immediate_quote(session,monkeypatch,code="000003",at=at,price=10)

    standard_response = await client.post("/trading/orders", json={
        "code": "000003",
        "side": "buy",
        "price": 10.0,
        "quantity": 100,
        "broker": "paper",
        "strategy_id": "manual",
    })
    assert standard_response.status_code == 200
    assert standard_response.json()["order"]["status"] == "risk_blocked"

    async with SessionLocal() as session:
        recovery_result = await trading.submit_order(
            session,
            trading.SubmitOrderCommand(
                code="000003",
                side="buy",
                price=10.0,
                quantity=100,
                broker="paper",
                strategy_id="paper-auto-short",
                is_drawdown_recovery_probe=True,
                drawdown_recovery_limit_pct=20.0,
                decision_at=at,
            ),
        )

    assert recovery_result["order"]["status"] == "filled"
    assert recovery_result["risk"]["final_level"] == "warn"
    assert any(
        item["rule"] == "max_drawdown"
        for item in recovery_result["risk"]["warnings"]
    )


@pytest.mark.asyncio
async def test_limit_up_queue_waits_for_fifo_turnover_before_fill(trading_client, monkeypatch):
    """封板可报队列，但新增成交量未覆盖前方买一时不能虚假成交。"""
    # FIFO mechanics fixture; actual automated-entry validity is tested separately.
    monkeypatch.setattr(trading_service, "_requires_pending_buy_validity", lambda _order: False)
    _client, SessionLocal = trading_client
    from paper_pending_fixture import accepted_frame
    from test_quote_round_execution import _round_payload
    queue_time = datetime(2026, 9, 14, 10)
    async with SessionLocal() as session:
        sentiment = (await session.scalars(select(MarketSentiment))).one()
        sentiment.trade_date = queue_time.date()
        async def frame(at, round_id):
            spot = await session.get(StockSpot, "600500")
            payload = _round_payload(round_id, at, [{
                key: getattr(spot, key) for key in ("code", "name", "price", "prev_close",
                    "open", "low", "limit_up", "volume", "bid1_price", "bid1_volume", "ask1_price")
            }])
            await accepted_frame(session, payload)
            monkeypatch.setattr(paper, "_quote_round_context", lambda: payload)
            monkeypatch.setattr(paper, "_paper_now", lambda: at)
            monkeypatch.setattr(paper, "_public_order_clock", lambda: at)
        session.add(StockSpot(
            code="600500",
            name="回封排队样本",
            price=11.0,
            prev_close=10.0,
            open=10.5,
            low=10.4,
            limit_up=11.0,
            volume=50_000,
            bid1_price=11.0,
            bid1_volume=1_000,
            ask1_price=0,
            updated_at=queue_time,
        ))
        await session.commit()

        await frame(queue_time, "fifo-decision")
        queued = await trading_service.submit_order(
            session,
            trading_service.SubmitOrderCommand(
                code="600500",
                side="buy",
                price=11.0,
                quantity=100,
                broker="paper",
                account_id="tenbagger",
                strategy_id="paper-auto-short",
                signal_id=f"queue-{queue_time:%Y%m%d}-600500",
                decision_at=queue_time, decision_round_id="fifo-decision",
                source="tenbagger_midline",
                reason="高标回封涨停排队",
                queue_if_limit_up=True,
                queue_metadata={
                    "cancel_time": "14:50",
                    "stop_loss_price": 10.34,
                    "block_warn": True,
                },
            ),
        )
        assert queued["order"]["status"] == "submitted"
        assert queued["fills"] == []
        assert (
            queued["risk"]["paper_limit_up_queue"]["strategy_version"]
            == paper._strategy_version("tenbagger")
        )

        spot = await session.get(StockSpot, "600500")
        spot.volume = 51_000  # 仅覆盖前方1000手，尚未覆盖本单1手
        spot.updated_at = queue_time.replace(minute=1)
        await session.commit()
        await frame(queue_time.replace(minute=1), "fifo-wait")
        waiting = await trading_service.reconcile_paper_limit_up_orders(
            session,
            account_id="tenbagger",
            now=queue_time.replace(minute=1),
        )
        assert waiting[0]["event"] == "waiting"
        assert "1000手/需覆盖1001手" in waiting[0]["reason"]

        spot = await session.get(StockSpot, "600500")
        spot.volume = 51_001
        spot.updated_at = queue_time.replace(minute=2)
        await session.commit()
        await frame(queue_time.replace(minute=2), "fifo-fill")
        filled = await trading_service.reconcile_paper_limit_up_orders(
            session,
            account_id="tenbagger",
            now=queue_time.replace(minute=2),
        )

        assert filled[0]["event"] == "filled"
        assert filled[0]["order"]["status"] == "filled"
        assert filled[0]["order"]["filled_quantity"] == 100
        assert filled[0]["fills"][0]["price"] == 11.0
        position = (
            await session.execute(
                paper_models.PaperPosition.__table__.select().where(
                    paper_models.PaperPosition.code == "600500"
                )
            )
        ).first()
        assert position is not None
        assert (
            position.strategy_version
            == paper._strategy_version("tenbagger")
        )


@pytest.mark.asyncio
async def test_limit_up_queue_fails_closed_after_strategy_version_change(
    trading_client,
    monkeypatch,
):
    _client, SessionLocal = trading_client
    queue_time = datetime.combine(date.today(), time(10, 0))
    original_version = paper._strategy_version("tenbagger")
    original_base_version = trading_service.settings.PAPER_STRATEGY_E_VERSION
    async with SessionLocal() as session:
        session.add(
            StockSpot(
                code="600503",
                name="版本切换排队",
                price=11.0,
                prev_close=10.0,
                open=10.5,
                low=10.4,
                limit_up=11.0,
                volume=50_000,
                bid1_price=11.0,
                bid1_volume=1_000,
                ask1_price=0,
                updated_at=queue_time,
            )
        )
        await session.commit()
        queued = await trading_service.submit_order(
            session,
            trading_service.SubmitOrderCommand(
                code="600503",
                side="buy",
                price=11.0,
                quantity=100,
                account_id="tenbagger",
                strategy_id="paper-auto-short",
                source="tenbagger_midline",
                queue_if_limit_up=True,
            ),
        )
        assert queued["order"]["status"] == "submitted"
        assert (
            queued["risk"]["paper_limit_up_queue"]["strategy_version"]
            == original_version
        )

        monkeypatch.setattr(
            trading_service.settings,
            "PAPER_STRATEGY_E_VERSION",
            f"{original_base_version}_next",
        )
        outcomes = await trading_service.reconcile_paper_limit_up_orders(
            session,
            account_id="tenbagger",
            now=queue_time.replace(minute=1),
        )

        assert outcomes[0]["event"] == "risk_blocked"
        assert outcomes[0]["order"]["status"] == "risk_blocked"
        assert "禁止跨版本成交" in outcomes[0]["reason"]
        positions = (
            await session.execute(
                paper_models.PaperPosition.__table__.select().where(
                    paper_models.PaperPosition.code == "600503"
                )
            )
        ).all()
        assert positions == []


@pytest.mark.asyncio
async def test_limit_up_queue_cancels_at_cutoff_without_position(trading_client):
    _client, SessionLocal = trading_client
    queue_time = datetime.combine(date.today(), time(14, 40))
    async with SessionLocal() as session:
        session.add(StockSpot(
            code="600501",
            name="排队撤单样本",
            price=11.0,
            prev_close=10.0,
            open=10.6,
            low=10.5,
            limit_up=11.0,
            volume=20_000,
            bid1_price=11.0,
            bid1_volume=50_000,
            ask1_price=0,
            updated_at=queue_time,
        ))
        await session.commit()
        queued = await trading_service.submit_order(
            session,
            trading_service.SubmitOrderCommand(
                code="600501",
                side="buy",
                price=11.0,
                quantity=100,
                account_id="tenbagger",
                source="tenbagger_midline",
                queue_if_limit_up=True,
                queue_metadata={"cancel_time": "14:50"},
            ),
        )

        outcomes = await trading_service.reconcile_paper_limit_up_orders(
            session,
            account_id="tenbagger",
            now=queue_time.replace(minute=51),
        )

        assert queued["order"]["status"] == "submitted"
        assert outcomes[0]["event"] == "canceled"
        assert outcomes[0]["order"]["status"] == "canceled"
        assert "14:50" in outcomes[0]["reason"]
        positions = (
            await session.execute(
                paper_models.PaperPosition.__table__.select().where(
                    paper_models.PaperPosition.code == "600501"
                )
            )
        ).all()
        assert positions == []


@pytest.mark.asyncio
async def test_paper_limit_up_queue_supports_manual_cancel(trading_client):
    _client, SessionLocal = trading_client
    async with SessionLocal() as session:
        session.add(StockSpot(
            code="600502",
            name="手动撤排队",
            price=11.0,
            prev_close=10.0,
            open=10.5,
            low=10.4,
            limit_up=11.0,
            volume=10_000,
            bid1_price=11.0,
            bid1_volume=30_000,
            ask1_price=0,
            updated_at=datetime.now(),
        ))
        await session.commit()
        queued = await trading_service.submit_order(
            session,
            trading_service.SubmitOrderCommand(
                code="600502",
                side="buy",
                price=11.0,
                quantity=100,
                account_id="tenbagger",
                source="tenbagger_midline",
                queue_if_limit_up=True,
            ),
        )
        canceled = await trading_service.cancel_order(
            session,
            queued["order"]["order_id"],
        )

        assert canceled["status"] == "canceled"
        assert canceled["order"]["status"] == "canceled"
        assert "涨停排队" in canceled["reason"]
