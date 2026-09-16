import json
from datetime import date, datetime, timedelta
from unittest.mock import AsyncMock

from challenger_execution_fixture import qualified_challenger_execution

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.v1 import paper
from app.config.settings import settings
from app.db.session import Base
from app.models import paper as paper_models  # noqa: F401
from app.models import signal as signal_models  # noqa: F401
from app.models import stock as stock_models  # noqa: F401
from app.models import trading as trading_models  # noqa: F401
from app.models.paper import (
    PaperAutoTradeLog,
    PaperNav,
    PaperPosition,
    PaperShadowEvaluation,
    PaperShadowEvent,
    PaperTradeLog,
)
from app.models.stock import MarketSentiment, StockSpot, StockTag
from app.models.trading import TradeOrder
from app.paper.strategy_iteration_challenger import (
    _common_period_comparison,
    _signal_token,
    build_strategy_iteration_challenger_comparison,
    run_strategy_iteration_challenger_accounts,
)
from app.paper.strategy_iteration_shadow import (
    ROUTE_B,
    ROUTE_C,
    ROUTE_C3,
    ROUTE_D,
    ROUTE_F2,
    route_version_for,
)


@pytest_asyncio.fixture
async def challenger_env(tmp_path, monkeypatch):
    # Local Sep-2026 test calendar only; no production or network fallback.
    start = date(2026, 8, 1)
    monkeypatch.setattr(paper.trade_calendar, "_cache", {
        start + timedelta(days=i): (start + timedelta(days=i)).weekday() < 5
        for i in range(61)
    })
    async def local_loaded(self, year):
        assert self is paper.trade_calendar and year == 2026
    # Patch the defining class: restoring an instance bound method can retain
    # another fixture\'s temporary class method as a permanent instance shadow.
    monkeypatch.setattr(type(paper.trade_calendar), "_ensure_loaded", local_loaded)
    calendar_network = AsyncMock(side_effect=AssertionError("Challenger fixture forbids calendar network"))
    monkeypatch.setattr(type(paper.trade_calendar), "_sync_from_source", calendar_network)
    # Most execution tests exercise the enabled path explicitly. Production
    # defaults may pause individual research routes, so make that precondition
    # local to this fixture instead of relying on a dead configuration flag.
    monkeypatch.setattr(settings, "PAPER_CONTINUOUS_EXPERIMENT_ENABLED", False)
    for setting_name in (
        "PAPER_CHALLENGER_A_AUTO_ORDER_ENABLED",
        "PAPER_CHALLENGER_B_AUTO_ORDER_ENABLED",
        "PAPER_CHALLENGER_C_AUTO_ORDER_ENABLED",
        "PAPER_CHALLENGER_D_AUTO_ORDER_ENABLED",
        "PAPER_CHALLENGER_F2_AUTO_ORDER_ENABLED",
    ):
        monkeypatch.setattr(settings, setting_name, True)
    # 本文件聚焦挑战者路由与隔离账户；跨轮次撮合在专用测试中验证。
    monkeypatch.setattr(settings, "PAPER_DEFER_AUTO_FILL_TO_NEXT_ROUND", False)
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'strategy-challenger.db'}",
        future=True,
    )
    SessionLocal = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with SessionLocal() as session:
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
                calculation_version="test_v1",
            )
        )
        await session.commit()
    yield SessionLocal
    await engine.dispose()
    calendar_network.assert_not_awaited()


def _snapshot(
    code: str,
    now: datetime,
    *,
    route_id: str = ROUTE_B,
    price: float = 10.05,
    ask: float = 10.06,
    volume_ratio: float = 1.2,
    orderbook_imbalance: float = 0.15,
) -> str:
    prior_structure = {"previous_consecutive_days": 1}
    if route_id == ROUTE_D:
        prior_structure.update(
            {
                "auction_volume_path_verified": True,
                "cancel_phase_verified": True,
            }
        )
    rule_snapshot = {"champion_order_connected": False}
    if route_id == "momentum_first_retest":
        rule_snapshot.update({
            "candidate_min_change_pct": 3.0,
            "candidate_max_change_pct": 6.0,
            "max_volume_ratio": 5.0,
            "min_orderbook_imbalance": -0.2,
            "max_withdrawal_ratio": 0.5,
        })
    return json.dumps(
        {
            "as_of_at": now.isoformat(),
            "point_in_time_only": True,
            "quote": {
                **({"amount": 50_000_000, "withdrawal_ratio": 0.1}
                   if route_id == "momentum_first_retest" else {}),
                "code": code,
                "name": f"测试{code}",
                "price": price,
                "prev_close": 10.0,
                "open": 9.8,
                "high": max(price, 10.1),
                "low": 9.7,
                "change_pct": (price / 10.0 - 1) * 100,
                "avg_price": 10.02,
                "volume_ratio": volume_ratio,
                "orderbook_imbalance": orderbook_imbalance,
                "ask1_price": ask,
                "ask1_volume": 500,
                "bid1_price": 10.04,
                "limit_up": 11.0,
                "limit_down": 9.0,
                "updated_at": now.isoformat(),
            },
            "rule_snapshot": rule_snapshot,
            "state": {"peak_price": max(price, 10.4)} if route_id == "momentum_first_retest" else {},
            "prior_structure": prior_structure,
        },
        ensure_ascii=False,
    )


async def _seed_confirmed(
    session: AsyncSession,
    *,
    code: str,
    route_id: str,
    now: datetime,
    price: float = 10.05,
    ask: float = 10.06,
    ask_volume: float = 500,
    limit_up: float = 11.0,
    volume_ratio: float = 1.2,
    orderbook_imbalance: float = 0.15,
) -> PaperShadowEvent:
    session.add(
        StockTag(
            code=code,
            name=f"测试{code}",
            board_type="main_sh",
            board_tag="tradeable",
            is_st=False,
            is_suspended=False,
            is_delisting=False,
            is_ipo_recent=False,
        )
    )
    session.add(
        StockSpot(
            code=code,
            name=f"测试{code}",
            price=price,
            prev_close=10.0,
            open=9.8,
            high=max(price, 10.1),
            low=9.7,
            avg_price=10.02,
            change_pct=(price / 10.0 - 1) * 100,
            volume_ratio=volume_ratio,
            orderbook_imbalance=orderbook_imbalance,
            ask1_price=ask,
            ask1_volume=ask_volume,
            bid1_price=max(price - 0.01, 0.01),
            bid1_volume=800,
            # A2基准样本必须具备可执行的真实数值，缺失值另有拒绝测试。
            amount=50_000_000 if route_id == "momentum_first_retest" else None,
            withdrawal_ratio=0.1 if route_id == "momentum_first_retest" else None,
            limit_up=limit_up,
            limit_down=9.0,
            updated_at=now,
        )
    )
    route_version = (
        settings.PAPER_MOMENTUM_RETEST_SHADOW_VERSION
        if route_id == "momentum_first_retest"
        else route_version_for(route_id)
    )
    event = PaperShadowEvent(
        event_key=f"{route_id}:{route_version}:{now.date().isoformat()}:{code}:confirmed",
        route_id=route_id,
        route_version=route_version,
        trade_date=now.date(),
        observed_at=now,
        code=code,
        name=f"测试{code}",
        event_type="confirmed",
        status="confirmed",
        price=price,
        assumed_fill_price=max(price, ask) * 1.001,
        change_pct=(price / 10.0 - 1) * 100,
        snapshot_json=_snapshot(
            code,
            now,
            route_id=route_id,
            price=price,
            ask=ask,
            volume_ratio=volume_ratio,
            orderbook_imbalance=orderbook_imbalance,
        ),
        created_at=now,
    )
    session.add(event)
    await session.commit()
    return event


@pytest.mark.asyncio
async def test_confirmed_event_executes_once_in_isolated_challenger_account(
    challenger_env, qualified_challenger_execution, monkeypatch,
):
    SessionLocal = challenger_env
    now = datetime(2026, 9, 1, 10, 0, 0)

    async def pass_risk(*_args, **_kwargs):
        return {"final_level": "pass", "block_reasons": [], "warnings": []}

    monkeypatch.setattr(paper, "_risk_check_for_buy", pass_risk)
    async with SessionLocal() as session:
        await _seed_confirmed(session, code="600100", route_id=ROUTE_B, now=now)
        first = await qualified_challenger_execution(session, now=now)
        second = await qualified_challenger_execution(session, now=now)

        challenger = await paper._get_or_create_account(session, paper.PAPER_ACCOUNT_CHALLENGER_B)
        champion = await paper._get_or_create_account(session, paper.PAPER_ACCOUNT_PROMOTION)
        challenger_positions = list(
            (
                await session.scalars(
                    select(PaperPosition).where(
                        PaperPosition.account_id == challenger.id,
                        PaperPosition.is_closed.is_(False),
                    )
                )
            ).all()
        )
        champion_position_count = await session.scalar(
            select(func.count(PaperPosition.id)).where(PaperPosition.account_id == champion.id)
        )
        trade_count = await session.scalar(
            select(func.count(PaperTradeLog.id)).where(PaperTradeLog.account_id == challenger.id)
        )
        comparison = await build_strategy_iteration_challenger_comparison(
            session,
            horizon_days=3,
            account_name=paper.PAPER_ACCOUNT_PROMOTION,
        )

    assert set(paper.PAPER_CHALLENGER_ACCOUNTS).isdisjoint(paper.PAPER_ALL_ACCOUNTS)
    assert first["entries"] == 1
    assert second["entries"] == 0
    assert len(challenger_positions) == 1
    assert challenger_positions[0].code == "600100"
    assert challenger_positions[0].stop_loss_price is not None
    assert challenger_positions[0].strategy_version == paper._strategy_version(
        paper.PAPER_ACCOUNT_CHALLENGER_B
    )
    assert champion_position_count == 0
    assert trade_count == 1
    execution_summary = comparison["pairs"][0]["challenger"][
        "current_version_execution"
    ]
    assert execution_summary["trade_count"] == 1
    assert execution_summary["buy_trade_count"] == 1
    assert execution_summary["sell_trade_count"] == 0
    assert execution_summary["open_position_count"] == 1
    assert first["broker"] == "paper"
    assert first["real_order_connected"] is False


@pytest.mark.asyncio
async def test_disabled_route_collects_evidence_without_opening_position(
    challenger_env,
    monkeypatch,
):
    SessionLocal = challenger_env
    now = datetime(2026, 9, 1, 10, 0, 0)
    monkeypatch.setattr(settings, "PAPER_CHALLENGER_C_AUTO_ORDER_ENABLED", False)

    async def pass_risk(*_args, **_kwargs):
        return {"final_level": "pass", "block_reasons": [], "warnings": []}

    monkeypatch.setattr(paper, "_risk_check_for_buy", pass_risk)
    async with SessionLocal() as session:
        event = await _seed_confirmed(
            session,
            code="600117",
            route_id=ROUTE_C,
            now=now,
        )
        first = await run_strategy_iteration_challenger_accounts(session, now=now)
        second = await run_strategy_iteration_challenger_accounts(session, now=now)
        account = await paper._get_or_create_account(
            session,
            paper.PAPER_ACCOUNT_CHALLENGER_C,
        )
        position_count = await session.scalar(
            select(func.count(PaperPosition.id)).where(
                PaperPosition.account_id == account.id
            )
        )
        logs = list(
            (
                await session.scalars(
                    select(PaperAutoTradeLog).where(
                        PaperAutoTradeLog.account_id == account.id,
                        PaperAutoTradeLog.code == event.code,
                    )
                )
            ).all()
        )

    assert first["entries"] == 0
    assert first["skipped"] == 1
    assert second["skipped"] == 0
    assert position_count == 0
    assert len(logs) == 1
    assert logs[0].decision == "dry_run"
    assert "自动撮合已暂停" in logs[0].reason


@pytest.mark.asyncio
async def test_superseded_single_frame_position_exits_next_sellable_session(
    challenger_env, qualified_challenger_execution,
    monkeypatch,
):
    SessionLocal = challenger_env
    entry_at = datetime(2026, 9, 1, 10, 0, 0)
    exit_at = datetime(2026, 9, 2, 9, 40, 0)

    async def pass_risk(*_args, **_kwargs):
        return {"final_level": "pass", "block_reasons": [], "warnings": []}

    monkeypatch.setattr(paper, "_risk_check_for_buy", pass_risk)
    async with SessionLocal() as session:
        await _seed_confirmed(
            session,
            code="600116",
            route_id=ROUTE_B,
            now=entry_at,
        )
        entered = await qualified_challenger_execution(
            session,
            now=entry_at,
        )
        account = await paper._get_or_create_account(
            session,
            paper.PAPER_ACCOUNT_CHALLENGER_B,
        )
        position = await session.scalar(
            select(PaperPosition).where(
                PaperPosition.account_id == account.id,
                PaperPosition.code == "600116",
                PaperPosition.is_closed.is_(False),
            )
        )
        buy_trade = await session.scalar(
            select(PaperTradeLog).where(
                PaperTradeLog.account_id == account.id,
                PaperTradeLog.code == "600116",
                PaperTradeLog.trade_type == "buy",
            )
        )
        spot = await session.scalar(
            select(StockSpot).where(StockSpot.code == "600116")
        )
        position.buy_time = entry_at
        buy_trade.trade_time = entry_at
        buy_trade.reason = (
            f"旧单帧事件={ROUTE_B}:abcdef_shape_v1:"
            "2026-09-01:600116:confirmed"
        )
        position.strategy_version = "abcdef_shape_v1:b_weak_open_second_board"
        buy_trade.strategy_version = "abcdef_shape_v1:b_weak_open_second_board"
        spot.updated_at = exit_at
        spot.price = 9.9
        spot.bid1_price = 9.89
        await session.commit()

        exited = await qualified_challenger_execution(
            session,
            now=exit_at,
        )
        open_count = await session.scalar(
            select(func.count(PaperPosition.id)).where(
                PaperPosition.account_id == account.id,
                PaperPosition.code == "600116",
                PaperPosition.is_closed.is_(False),
            )
        )
        sell_trade = await session.scalar(
            select(PaperTradeLog).where(
                PaperTradeLog.account_id == account.id,
                PaperTradeLog.code == "600116",
                PaperTradeLog.trade_type == "sell",
            )
        )
        exit_logs = list(
            (
                await session.scalars(
                    select(PaperAutoTradeLog).where(
                        PaperAutoTradeLog.account_id == account.id,
                        PaperAutoTradeLog.code == "600116",
                    )
                )
            ).all()
        )
        comparison = await build_strategy_iteration_challenger_comparison(
            session,
            horizon_days=3,
            account_name=paper.PAPER_ACCOUNT_PROMOTION,
        )
        execution_summary = comparison["pairs"][0]["challenger"][
            "current_version_execution"
        ]

    assert entered["entries"] == 1
    assert exited["sells"] == 1, [
        (row.action, row.decision, row.reason) for row in exit_logs
    ]
    assert open_count == 0
    assert sell_trade is not None
    assert "旧版或未标版本仓位隔离退出" in sell_trade.reason
    assert execution_summary["trade_count"] == 0
    assert execution_summary["forced_legacy_exit_count"] == 1


@pytest.mark.asyncio
async def test_superseded_multiframe_position_is_grandfathered_to_normal_exit_rules(
    challenger_env, qualified_challenger_execution,
    monkeypatch,
):
    SessionLocal = challenger_env
    entry_at = datetime(2026, 9, 3, 10, 0, 0)
    manage_at = datetime(2026, 9, 4, 9, 40, 0)

    async def pass_risk(*_args, **_kwargs):
        return {"final_level": "pass", "block_reasons": [], "warnings": []}

    monkeypatch.setattr(paper, "_risk_check_for_buy", pass_risk)
    async with SessionLocal() as session:
        await _seed_confirmed(
            session,
            code="600318",
            route_id=ROUTE_C,
            now=entry_at,
        )
        entered = await qualified_challenger_execution(
            session,
            now=entry_at,
        )
        account = await paper._get_or_create_account(
            session,
            paper.PAPER_ACCOUNT_CHALLENGER_C,
        )
        position = await session.scalar(
            select(PaperPosition).where(
                PaperPosition.account_id == account.id,
                PaperPosition.code == "600318",
                PaperPosition.is_closed.is_(False),
            )
        )
        buy_trade = await session.scalar(
            select(PaperTradeLog).where(
                PaperTradeLog.account_id == account.id,
                PaperTradeLog.code == "600318",
                PaperTradeLog.trade_type == "buy",
            )
        )
        spot = await session.scalar(
            select(StockSpot).where(StockSpot.code == "600318")
        )
        previous_version = (
            "abcdef_shape_v3_relative_strength:c_recent_limit_relaunch"
        )
        position.buy_time = entry_at
        position.strategy_version = previous_version
        buy_trade.trade_time = entry_at
        buy_trade.strategy_version = previous_version
        spot.updated_at = manage_at
        spot.price = 9.9
        spot.bid1_price = 9.89
        await session.commit()

        managed = await qualified_challenger_execution(
            session,
            now=manage_at,
        )
        open_count = await session.scalar(
            select(func.count(PaperPosition.id)).where(
                PaperPosition.account_id == account.id,
                PaperPosition.code == "600318",
                PaperPosition.is_closed.is_(False),
            )
        )
        sell_trade = await session.scalar(
            select(PaperTradeLog).where(
                PaperTradeLog.account_id == account.id,
                PaperTradeLog.code == "600318",
                PaperTradeLog.trade_type == "sell",
            )
        )
        management_logs = list(
            (
                await session.scalars(
                    select(PaperAutoTradeLog).where(
                        PaperAutoTradeLog.account_id == account.id,
                        PaperAutoTradeLog.code == "600318",
                        PaperAutoTradeLog.trade_date == manage_at.date(),
                    )
                )
            ).all()
        )

    assert entered["entries"] == 1
    assert managed["sells"] == 0
    assert open_count == 1
    assert sell_trade is None
    assert any(log.action == "hold" for log in management_logs)
    assert all(
        "旧版或未标版本仓位隔离退出" not in str(log.reason or "")
        for log in management_logs
    )


@pytest.mark.asyncio
async def test_confirmed_event_waits_for_route_batch_gate_then_executes(
    challenger_env, qualified_challenger_execution,
    monkeypatch,
):
    SessionLocal = challenger_env
    confirmed_at = datetime(2026, 9, 1, 9, 26, 0)
    defer_at = datetime(2026, 9, 1, 9, 30, 0)
    execute_at = datetime(2026, 9, 1, 9, 35, 0)

    async def pass_risk(*_args, **_kwargs):
        return {"final_level": "pass", "block_reasons": [], "warnings": []}

    monkeypatch.setattr(paper, "_risk_check_for_buy", pass_risk)
    async with SessionLocal() as session:
        await _seed_confirmed(
            session,
            code="600106",
            route_id=ROUTE_C,
            now=confirmed_at,
        )
        spot = await session.scalar(
            select(StockSpot).where(StockSpot.code == "600106")
        )
        spot.updated_at = defer_at
        await session.commit()
        deferred = await qualified_challenger_execution(
            session,
            now=defer_at,
        )
        spot.updated_at = execute_at
        await session.commit()
        executed = await qualified_challenger_execution(
            session,
            now=execute_at,
        )

    assert deferred["entries"] == 0
    assert deferred["deferred"] == 1
    assert executed["entries"] == 1


@pytest.mark.asyncio
async def test_same_route_daily_slot_uses_point_in_time_priority_not_insert_id(
    challenger_env, qualified_challenger_execution,
    monkeypatch,
):
    SessionLocal = challenger_env
    now = datetime(2026, 9, 1, 9, 40, 0)

    async def pass_risk(*_args, **_kwargs):
        return {"final_level": "pass", "block_reasons": [], "warnings": []}

    monkeypatch.setattr(paper, "_risk_check_for_buy", pass_risk)
    monkeypatch.setattr(paper, "_strategy_buy_limits", lambda _name: (1, 1))
    async with SessionLocal() as session:
        # 低质量事件先插入，确保排序不是数据库主键顺序。
        await _seed_confirmed(
            session,
            code="600107",
            route_id=ROUTE_C,
            now=now,
            price=10.03,
            ask=10.04,
            volume_ratio=0.8,
            orderbook_imbalance=-0.1,
        )
        await _seed_confirmed(
            session,
            code="600108",
            route_id=ROUTE_C,
            now=now,
            price=10.20,
            ask=10.21,
            volume_ratio=2.5,
            orderbook_imbalance=1.0,
        )
        result = await qualified_challenger_execution(
            session,
            now=now,
        )
        account = await paper._get_or_create_account(
            session,
            paper.PAPER_ACCOUNT_CHALLENGER_C,
        )
        positions = list(
            (
                await session.scalars(
                    select(PaperPosition).where(
                        PaperPosition.account_id == account.id,
                        PaperPosition.is_closed.is_(False),
                    )
                )
            ).all()
        )

    assert result["entries"] == 1
    assert [row.code for row in positions] == ["600108"]


@pytest.mark.parametrize(
    ("route_id", "challenger_name", "champion_name", "code"),
    [
        (ROUTE_B, paper.PAPER_ACCOUNT_CHALLENGER_B, paper.PAPER_ACCOUNT_PROMOTION, "600110"),
        (ROUTE_C, paper.PAPER_ACCOUNT_CHALLENGER_C, paper.PAPER_ACCOUNT_MAINLINE, "600111"),
        (ROUTE_D, paper.PAPER_ACCOUNT_CHALLENGER_D, paper.PAPER_ACCOUNT_AUCTION, "600112"),
        (ROUTE_F2, paper.PAPER_ACCOUNT_CHALLENGER_F2, paper.PAPER_ACCOUNT_REVERSAL, "600113"),
    ],
)
@pytest.mark.asyncio
async def test_each_configured_route_writes_only_its_own_isolated_account(
    challenger_env, qualified_challenger_execution,
    monkeypatch,
    route_id,
    challenger_name,
    champion_name,
    code,
):
    SessionLocal = challenger_env
    now = datetime(2026, 9, 1, 10, 12, 0)

    async def pass_risk(*_args, **_kwargs):
        return {"final_level": "pass", "block_reasons": [], "warnings": []}

    monkeypatch.setattr(paper, "_risk_check_for_buy", pass_risk)
    async with SessionLocal() as session:
        await _seed_confirmed(session, code=code, route_id=route_id, now=now)
        result = await qualified_challenger_execution(session, now=now)

        account_ids = {}
        for account_name in (*paper.PAPER_ALL_ACCOUNTS, *paper.PAPER_CHALLENGER_ACCOUNTS):
            account = await paper._get_or_create_account(session, account_name)
            account_ids[account_name] = account.id
        position_rows = list((await session.scalars(select(PaperPosition))).all())
        position_count_by_account = {
            account_name: sum(row.account_id == account_id for row in position_rows)
            for account_name, account_id in account_ids.items()
        }

    assert result["entries"] == 1
    assert position_count_by_account[challenger_name] == 1
    assert all(
        position_count_by_account[account_name] == 0
        for account_name in paper.PAPER_ALL_ACCOUNTS
    )
    assert all(
        position_count_by_account[account_name] == 0
        for account_name in paper.PAPER_CHALLENGER_ACCOUNTS
        if account_name != challenger_name
    )
    assert paper.PAPER_CHALLENGER_BASE_ACCOUNT[challenger_name] == champion_name


@pytest.mark.asyncio
async def test_limit_up_without_offer_waits_for_new_round_not_fake_fill(
    challenger_env,
):
    SessionLocal = challenger_env
    now = datetime(2026, 9, 1, 10, 5, 0)
    async with SessionLocal() as session:
        event = await _seed_confirmed(
            session,
            code="600101",
            route_id=ROUTE_C,
            now=now,
            price=11.0,
            ask=0.0,
            limit_up=11.0,
        )
        result = await run_strategy_iteration_challenger_accounts(session, now=now)
        challenger = await paper._get_or_create_account(session, paper.PAPER_ACCOUNT_CHALLENGER_C)
        position_count = await session.scalar(
            select(func.count(PaperPosition.id)).where(PaperPosition.account_id == challenger.id)
        )
        log = await session.scalar(
            select(PaperAutoTradeLog).where(
                PaperAutoTradeLog.account_id == challenger.id,
                PaperAutoTradeLog.code == event.code,
            )
        )

    assert result["entries"] == 0
    assert result["skipped"] == 1
    assert position_count == 0
    assert log is not None
    assert log.decision == "wait"
    assert "不可成交" in log.reason


@pytest.mark.asyncio
async def test_positive_ask_price_without_offer_volume_is_not_filled(challenger_env):
    SessionLocal = challenger_env
    now = datetime(2026, 9, 1, 10, 6, 0)
    async with SessionLocal() as session:
        event = await _seed_confirmed(
            session,
            code="600123",
            route_id=ROUTE_C,
            now=now,
            ask=10.06,
            ask_volume=0,
        )
        result = await run_strategy_iteration_challenger_accounts(session, now=now)
        challenger = await paper._get_or_create_account(
            session,
            paper.PAPER_ACCOUNT_CHALLENGER_C,
        )
        position_count = await session.scalar(
            select(func.count(PaperPosition.id)).where(
                PaperPosition.account_id == challenger.id
            )
        )
        log = await session.scalar(
            select(PaperAutoTradeLog).where(
                PaperAutoTradeLog.account_id == challenger.id,
                PaperAutoTradeLog.code == event.code,
            )
        )

    assert result["entries"] == 0
    assert result["skipped"] == 1
    assert position_count == 0
    assert log is not None
    assert "卖一量" in log.reason


@pytest.mark.asyncio
async def test_stale_confirmed_event_is_not_backfilled_at_old_price(challenger_env):
    SessionLocal = challenger_env
    observed_at = datetime(2026, 9, 1, 10, 0, 0)
    now = datetime(2026, 9, 1, 10, 13, 0)
    async with SessionLocal() as session:
        await _seed_confirmed(session, code="600102", route_id=ROUTE_B, now=observed_at)
        spot = await session.scalar(select(StockSpot).where(StockSpot.code == "600102"))
        spot.updated_at = now
        await session.commit()
        result = await run_strategy_iteration_challenger_accounts(session, now=now)
        challenger = await paper._get_or_create_account(session, paper.PAPER_ACCOUNT_CHALLENGER_B)
        position_count = await session.scalar(
            select(func.count(PaperPosition.id)).where(PaperPosition.account_id == challenger.id)
        )

    assert result["entries"] == 0
    assert result["skipped"] == 1
    assert position_count == 0


@pytest.mark.asyncio
async def test_other_backend_strategy_cannot_write_challenger_account(challenger_env, qualified_challenger_execution):
    from app.trading.service import SubmitOrderCommand, submit_order

    SessionLocal = challenger_env
    now = datetime(2026, 9, 1, 10, 8, 0)
    async with SessionLocal() as session:
        await _seed_confirmed(session, code="600105", route_id=ROUTE_B, now=now)
        result = await qualified_challenger_execution(
            session, now=now,
            command=SubmitOrderCommand(
                code="600105",
                side="buy",
                price=10.06,
                quantity=100,
                broker="paper",
                account_id=paper.PAPER_ACCOUNT_CHALLENGER_B,
                strategy_id="unrelated-backend-strategy",
                signal_id="not-a-challenger-signal",
                source="promotion_promotion",
                reason="不得写入隔离账户",
                execute=True,
                decision_at=now,
            ),
        )
        challenger = await paper._get_or_create_account(session, paper.PAPER_ACCOUNT_CHALLENGER_B)
        position_count = await session.scalar(
            select(func.count(PaperPosition.id)).where(PaperPosition.account_id == challenger.id)
        )

    assert result["order"]["status"] == "rejected"
    assert "只接受内部前向确认事件委托" in result["order"]["error_message"]
    assert position_count == 0


@pytest.mark.asyncio
async def test_d2_old_or_unverified_auction_evidence_cannot_execute(
    challenger_env,
):
    SessionLocal = challenger_env
    now = datetime(2026, 9, 1, 10, 8, 0)
    async with SessionLocal() as session:
        event = await _seed_confirmed(
            session,
            code="600114",
            route_id=ROUTE_D,
            now=now,
        )
        snapshot = json.loads(event.snapshot_json)
        snapshot["prior_structure"]["auction_volume_path_verified"] = False
        snapshot["prior_structure"]["cancel_phase_verified"] = False
        event.snapshot_json = json.dumps(snapshot, ensure_ascii=False)
        await session.commit()

        result = await run_strategy_iteration_challenger_accounts(
            session,
            now=now,
        )
        account = await paper._get_or_create_account(
            session,
            paper.PAPER_ACCOUNT_CHALLENGER_D,
        )
        log = await session.scalar(
            select(PaperAutoTradeLog).where(
                PaperAutoTradeLog.account_id == account.id,
                PaperAutoTradeLog.code == "600114",
            )
        )

    assert result["entries"] == 0
    assert result["skipped"] == 1
    assert log is not None
    assert "正量路径证据" in log.reason


@pytest.mark.asyncio
async def test_simulation_account_is_hard_blocked_from_nonpaper_broker(
    challenger_env,
):
    from fastapi import HTTPException
    from app.trading.service import SubmitOrderCommand, submit_order

    SessionLocal = challenger_env
    async with SessionLocal() as session:
        with pytest.raises(HTTPException) as captured:
            await submit_order(
                session,
                SubmitOrderCommand(
                    code="600115",
                    side="buy",
                    price=10.0,
                    quantity=100,
                    broker="live-adapter",
                    account_id=paper.PAPER_ACCOUNT_CHALLENGER_B,
                    strategy_id="paper-challenger-forward",
                    execute=True,
                ),
            )

    assert captured.value.status_code == 403
    assert "纯模拟账户" in str(captured.value.detail)


@pytest.mark.asyncio
async def test_warn_risk_is_blocked_in_challenger_auto_execution(
    challenger_env, monkeypatch,
):
    SessionLocal = challenger_env
    now = datetime(2026, 9, 1, 10, 10, 0)

    async def warn_risk(*_args, **_kwargs):
        return {"final_level": "warn", "block_reasons": [], "warnings": [{"message": "测试警告"}]}

    monkeypatch.setattr(paper, "_risk_check_for_buy", warn_risk)
    async with SessionLocal() as session:
        await _seed_confirmed(session, code="600104", route_id=ROUTE_B, now=now)
        result = await run_strategy_iteration_challenger_accounts(session, now=now)
        challenger = await paper._get_or_create_account(session, paper.PAPER_ACCOUNT_CHALLENGER_B)
        position_count = await session.scalar(
            select(func.count(PaperPosition.id)).where(PaperPosition.account_id == challenger.id)
        )

    assert result["entries"] == 0
    assert result["blocked"] == 1
    assert position_count == 0


@pytest.mark.asyncio
async def test_comparison_payload_pairs_champion_and_isolated_accounts(challenger_env):
    SessionLocal = challenger_env
    now = datetime(2026, 9, 1, 10, 0, 0)
    async with SessionLocal() as session:
        await _seed_confirmed(session, code="600103", route_id=ROUTE_B, now=now)
        payload = await build_strategy_iteration_challenger_comparison(
            session,
            horizon_days=3,
            recent_limit=20,
        )

    b_pair = next(item for item in payload["pairs"] if item["route_id"] == ROUTE_B)
    assert len(payload["pairs"]) == 6
    assert b_pair["champion"]["account_name"] == paper.PAPER_ACCOUNT_PROMOTION
    assert b_pair["challenger"]["account_name"] == paper.PAPER_ACCOUNT_CHALLENGER_B
    assert b_pair["challenger"]["is_challenger"] is True
    assert b_pair["challenger"]["execution_enabled"] is True
    assert b_pair["event_counts"]["confirmed"] == 1
    assert b_pair["evidence"]["confirmed_signal_count"] == 1
    assert b_pair["evidence"]["confirmed_signal_sessions"] == 1
    assert b_pair["evidence"]["pending_settlement_count"] == 1
    assert b_pair["evidence"]["pending_settlement_sessions"] == 1
    assert b_pair["evidence"]["settlement_state"] == "waiting_horizon"
    assert b_pair["evidence"]["collection_progress_pct"] == 1
    assert b_pair["evidence"]["settled_progress_pct"] == 0
    assert "不会" in payload["evidence_purpose"]["does_not"]
    assert payload["isolation"]["broker"] == "paper"
    assert payload["isolation"]["real_order_connected"] is False
    assert payload["isolation"]["shares_cash_positions_with_champion"] is False
    assert payload["isolation"]["promotion_requires_manual_review"] is True
    assert payload["isolation"]["minimum_sessions"] == 20
    assert payload["isolation"]["minimum_samples"] == 100
    assert payload["isolation"]["route_versions"][ROUTE_B] == route_version_for(ROUTE_B)
    assert payload["isolation"]["route_versions"][ROUTE_C3] == route_version_for(ROUTE_C3)
    assert route_version_for(ROUTE_C3) != settings.PAPER_FIRST_BOARD_SHADOW_VERSION
    assert payload["isolation"]["evidence_only_routes"] == [ROUTE_C3]
    assert (
        payload["isolation"]["maximum_account_drawdown_pct"]
        == settings.PAPER_STRATEGY_ITERATION_EVAL_MAX_ACCOUNT_DRAWDOWN_PCT
    )
    assert "account_drawdown_acceptable" not in b_pair["evidence"]["gates"]
    assert b_pair["evidence"]["execution_guardrails"][
        "account_drawdown_acceptable"
    ] is True
    assert b_pair["same_day_funnel"]["available"] is False
    assert b_pair["same_day_funnel"]["outcome_count"] is None
    assert b_pair["challenger"]["current_version_execution"]["trade_count"] == 0


@pytest.mark.asyncio
async def test_first_board_route_is_visible_but_has_no_execution_account(
    challenger_env,
):
    SessionLocal = challenger_env
    now = datetime(2026, 9, 4, 10, 0, 0)
    async with SessionLocal() as session:
        await _seed_confirmed(
            session,
            code="600122",
            route_id=ROUTE_C3,
            now=now,
            price=10.3,
            ask=10.31,
            volume_ratio=1.5,
            orderbook_imbalance=0.2,
        )
        execution = await run_strategy_iteration_challenger_accounts(
            session,
            now=now,
        )
        payload = await build_strategy_iteration_challenger_comparison(
            session,
            horizon_days=3,
            recent_limit=20,
            account_name=paper.PAPER_ACCOUNT_MAINLINE,
        )
        all_positions = await session.scalar(
            select(func.count()).select_from(PaperPosition)
        )
        all_trades = await session.scalar(
            select(func.count()).select_from(PaperTradeLog)
        )

    assert execution["events_considered"] == 0
    assert execution["entries"] == 0
    assert all_positions == 0
    assert all_trades == 0
    assert [item["route_id"] for item in payload["pairs"]] == [ROUTE_C, ROUTE_C3]
    c3_pair = next(item for item in payload["pairs"] if item["route_id"] == ROUTE_C3)
    assert c3_pair["execution_mode"] == "evidence_only"
    assert c3_pair["current_pool"]["read_model_version"] == "c3_current_pool_v1"
    assert c3_pair["current_pool"]["execution_enabled"] is False
    assert c3_pair["current_pool"]["confirmed_count"] == 0
    assert c3_pair["challenger"]["account_configured"] is False
    assert c3_pair["challenger"]["id"] is None
    assert c3_pair["challenger"]["execution_enabled"] is False
    assert c3_pair["evidence"]["confirmed_signal_count"] == 1
    assert c3_pair["evidence"]["account_drawdown_available"] is False
    assert c3_pair["evidence"]["account_drawdown_applicable"] is False
    assert "account_drawdown_acceptable" not in c3_pair["evidence"]["gates"]
    assert c3_pair["same_day_funnel"]["available"] is True
    assert payload["selected_strategy"]["route_count"] == 2
    assert payload["selected_strategy"]["routes"][1]["execution_mode"] == (
        "evidence_only"
    )


@pytest.mark.asyncio
async def test_first_board_comparison_exposes_fail_closed_activation_config(
    challenger_env,
    monkeypatch,
):
    monkeypatch.setattr(
        settings,
        "PAPER_FIRST_BOARD_SHADOW_ACTIVATION_DATE",
        "invalid-date",
    )
    SessionLocal = challenger_env
    async with SessionLocal() as session:
        payload = await build_strategy_iteration_challenger_comparison(
            session,
            horizon_days=3,
            account_name=paper.PAPER_ACCOUNT_MAINLINE,
        )

    c3_pair = next(item for item in payload["pairs"] if item["route_id"] == ROUTE_C3)
    assert c3_pair["activation_config_valid"] is False
    assert c3_pair["evidence"]["activation_config_valid"] is False
    assert c3_pair["evidence_status"] == "configuration_blocked"


@pytest.mark.asyncio
async def test_account_scoped_comparison_keeps_global_strategy_coverage_counts(
    challenger_env,
):
    SessionLocal = challenger_env
    now = datetime(2026, 9, 1, 10, 0, 0)
    async with SessionLocal() as session:
        await _seed_confirmed(session, code="600118", route_id=ROUTE_B, now=now)
        await _seed_confirmed(session, code="600119", route_id=ROUTE_C, now=now)
        payload = await build_strategy_iteration_challenger_comparison(
            session,
            horizon_days=3,
            recent_limit=20,
            account_name=paper.PAPER_ACCOUNT_PROMOTION,
        )

    coverage = {item["strategy_code"]: item for item in payload["strategy_coverage"]}
    assert [item["route_id"] for item in payload["pairs"]] == [ROUTE_B]
    assert coverage["B"]["confirmed_count"] == 1
    assert coverage["C"]["confirmed_count"] == 1
    assert coverage["C"]["implementation_status"] == "collecting"
    assert coverage["C"]["account_backed_route_count"] == 1
    assert coverage["C"]["execution_enabled_route_count"] == 1
    assert coverage["C"]["execution_paused_route_count"] == 0
    assert coverage["C"]["evidence_only_route_count"] == 1
    assert "1条使用独立模拟账户（1条开启撮合、0条暂停撮合）" in coverage["C"]["reason"]
    assert payload["coverage_summary"]["shadow_account_backed_route_count"] == 5
    assert payload["coverage_summary"]["account_backed_route_count"] == 6
    assert payload["coverage_summary"]["independent_execution_route_count"] == 1
    assert payload["coverage_summary"]["total_route_count"] == 7
    assert payload["coverage_summary"]["control_sample_route_count"] == 0
    assert payload["coverage_summary"]["independent_execution_enabled"] is True
    assert payload["coverage_summary"]["execution_enabled_route_count"] == 6
    assert payload["coverage_summary"]["execution_paused_route_count"] == 0
    assert payload["coverage_summary"]["evidence_only_route_count"] == 1


@pytest.mark.asyncio
async def test_comparison_excludes_superseded_route_and_action_versions(
    challenger_env,
):
    SessionLocal = challenger_env
    now = datetime(2026, 9, 2, 10, 0, 0)
    current_version = route_version_for(ROUTE_B)
    old_version = "abcdef_shape_v1"
    async with SessionLocal() as session:
        await _seed_confirmed(
            session,
            code="600120",
            route_id=ROUTE_B,
            now=now,
        )
        old_event_key = (
            f"{ROUTE_B}:{old_version}:{now.date().isoformat()}:600121:confirmed"
        )
        session.add(
            PaperShadowEvent(
                event_key=old_event_key,
                route_id=ROUTE_B,
                route_version=old_version,
                trade_date=now.date(),
                observed_at=now,
                code="600121",
                name="旧版本事件",
                event_type="confirmed",
                status="confirmed",
                price=10.0,
                assumed_fill_price=10.01,
                change_pct=1.0,
                snapshot_json="{}",
                created_at=now,
            )
        )
        session.add(
            PaperShadowEvaluation(
                signal_event_key=old_event_key,
                route_id=ROUTE_B,
                route_version=old_version,
                code="600121",
                signal_trade_date=now.date(),
                signal_time=now,
                horizon_days=3,
                exit_trade_date=now.date(),
                signal_price=10.01,
                exit_price=10.2,
                gross_return_pct=1.9,
                net_return_pct=1.5,
                benchmark_return_pct=0.2,
                excess_return_pct=1.3,
                max_favorable_pct=2.0,
                max_adverse_pct=-0.5,
                is_positive=True,
                details_json="{}",
                created_at=now,
            )
        )
        challenger = await paper._get_or_create_account(
            session,
            paper.PAPER_ACCOUNT_CHALLENGER_B,
        )
        challenger_c = await paper._get_or_create_account(
            session,
            paper.PAPER_ACCOUNT_CHALLENGER_C,
        )
        session.add_all(
            [
                PaperAutoTradeLog(
                    account_id=challenger.id,
                    run_id="old-version-action",
                    trade_date=now.date(),
                    created_at=now,
                    trigger="test",
                    source=ROUTE_B,
                    action="buy",
                    decision="executed",
                    strategy_version=f"{old_version}:{ROUTE_B}",
                ),
                PaperAutoTradeLog(
                    account_id=challenger.id,
                    run_id="current-version-action",
                    trade_date=now.date(),
                    created_at=now,
                    trigger="test",
                    source=ROUTE_B,
                    action="hold",
                    decision="wait",
                    strategy_version=paper._strategy_version(
                        paper.PAPER_ACCOUNT_CHALLENGER_B
                    ),
                ),
                PaperAutoTradeLog(
                    account_id=challenger_c.id,
                    run_id="cross-product-action-must-not-leak",
                    trade_date=now.date(),
                    created_at=now,
                    trigger="test",
                    source=ROUTE_C,
                    action="hold",
                    decision="wait",
                    # This version belongs to B, not the C account. Independent
                    # account/version IN predicates used to admit this row.
                    strategy_version=paper._strategy_version(
                        paper.PAPER_ACCOUNT_CHALLENGER_B
                    ),
                ),
            ]
        )
        await session.commit()

        payload = await build_strategy_iteration_challenger_comparison(
            session,
            horizon_days=3,
            recent_limit=20,
        )

    b_pair = next(item for item in payload["pairs"] if item["route_id"] == ROUTE_B)
    assert b_pair["event_counts"]["confirmed"] == 1
    assert b_pair["evidence"]["route_version"] == current_version
    assert b_pair["evidence"]["sample_count"] == 0
    assert all(
        item["route_version"] == current_version
        for item in payload["recent_signals"]
        if item["route_id"] == ROUTE_B
    )
    b_actions = [
        item for item in payload["recent_actions"] if item["source"] == ROUTE_B
    ]
    assert [item["run_id"] for item in b_actions] == ["current-version-action"]
    assert "cross-product-action-must-not-leak" not in {
        item["run_id"] for item in payload["recent_actions"]
    }


@pytest.mark.asyncio
async def test_recent_signal_limit_is_applied_after_route_version_scope(challenger_env):
    SessionLocal = challenger_env
    now = datetime(2026, 9, 2, 10, 0, 0)
    old_version = "abcdef_shape_v1"
    async with SessionLocal() as session:
        current = await _seed_confirmed(
            session,
            code="600124",
            route_id=ROUTE_B,
            now=now,
        )
        for index in range(5):
            observed_at = now.replace(minute=10 + index)
            session.add(
                PaperShadowEvent(
                    event_key=f"{ROUTE_B}:{old_version}:old-{index}",
                    route_id=ROUTE_B,
                    route_version=old_version,
                    trade_date=now.date(),
                    observed_at=observed_at,
                    code=f"600{930 + index}",
                    name=f"旧版本{index}",
                    event_type="confirmed",
                    status="confirmed",
                    price=10.0,
                    assumed_fill_price=10.01,
                    change_pct=1.0,
                    snapshot_json="{}",
                    created_at=observed_at,
                )
            )
        await session.commit()
        payload = await build_strategy_iteration_challenger_comparison(
            session,
            horizon_days=3,
            recent_limit=1,
            account_name=paper.PAPER_ACCOUNT_PROMOTION,
        )

    assert [row["event_key"] for row in payload["recent_signals"]] == [
        current.event_key
    ]


def test_common_period_return_does_not_compare_different_account_inception_dates():
    champion_rows = [
        PaperNav(account_id=1, trade_date=date(2026, 8, 31), nav=1.0, daily_return=0),
        PaperNav(account_id=1, trade_date=date(2026, 9, 1), nav=1.1, daily_return=10),
        PaperNav(account_id=1, trade_date=date(2026, 9, 2), nav=1.21, daily_return=10),
    ]
    challenger_rows = [
        PaperNav(account_id=2, trade_date=date(2026, 9, 1), nav=0.9, daily_return=0),
        PaperNav(account_id=2, trade_date=date(2026, 9, 2), nav=1.08, daily_return=20),
    ]

    result = _common_period_comparison(champion_rows, challenger_rows)

    assert result["comparable"] is True
    assert result["start_date"] == "2026-09-01"
    assert result["end_date"] == "2026-09-02"
    assert result["session_count"] == 2
    assert result["champion_return_pct"] == pytest.approx(10)
    assert result["challenger_return_pct"] == pytest.approx(20)
    assert result["return_delta_pct"] == pytest.approx(10)


@pytest.mark.asyncio
async def test_a2_consumes_only_momentum_shadow_version(challenger_env, qualified_challenger_execution, monkeypatch):
    SessionLocal = challenger_env
    now = datetime(2026, 9, 8, 10, 0, 0)

    async def pass_risk(*_args, **_kwargs):
        return {"final_level": "pass", "block_reasons": [], "warnings": []}

    monkeypatch.setattr(paper, "_risk_check_for_buy", pass_risk)
    async with SessionLocal() as session:
        event = await _seed_confirmed(
            session, code="600201", route_id="momentum_first_retest", now=now,
            price=10.4, ask=10.41, volume_ratio=1.5, orderbook_imbalance=0.2,
        )
        result = await qualified_challenger_execution(session, now=now)
        account = await paper._get_or_create_account(
            session, paper.PAPER_ACCOUNT_CHALLENGER_A
        )
        trade = await session.scalar(select(PaperTradeLog).where(
            PaperTradeLog.account_id == account.id,
            PaperTradeLog.code == event.code,
        ))

    assert event.route_version == settings.PAPER_MOMENTUM_RETEST_SHADOW_VERSION
    assert result["entries"] == 1
    assert trade is not None
    assert trade.strategy_version == paper._strategy_version(
        paper.PAPER_ACCOUNT_CHALLENGER_A
    )
    assert trade.strategy_version != event.route_version


@pytest.mark.asyncio
async def test_recoverable_offer_wait_retries_next_round_only(challenger_env, qualified_challenger_execution, monkeypatch):
    SessionLocal = challenger_env
    first_at = datetime(2026, 9, 8, 10, 1, 0)
    next_at = datetime(2026, 9, 8, 10, 1, 1)

    async def pass_risk(*_args, **_kwargs):
        return {"final_level": "pass", "block_reasons": [], "warnings": []}

    monkeypatch.setattr(paper, "_risk_check_for_buy", pass_risk)
    async with SessionLocal() as session:
        event = await _seed_confirmed(
            session, code="600202", route_id=ROUTE_B, now=first_at, ask_volume=0
        )
        first = await qualified_challenger_execution(session, now=first_at)
        same_round = await qualified_challenger_execution(session, now=first_at)
        spot = await session.scalar(select(StockSpot).where(StockSpot.code == event.code))
        spot.ask1_volume = 500
        spot.updated_at = next_at
        await session.commit()
        retried = await qualified_challenger_execution(session, now=next_at)
        account = await paper._get_or_create_account(
            session, paper.PAPER_ACCOUNT_CHALLENGER_B
        )
        event_logs = list((await session.scalars(select(PaperAutoTradeLog).where(
            PaperAutoTradeLog.account_id == account.id,
            PaperAutoTradeLog.code == event.code,
        ))).all())

    assert first["entries"] == 0
    assert same_round["skipped"] == 0
    assert retried["entries"] == 1
    assert [row.decision for row in event_logs] == ["wait", "executed"]


@pytest.mark.asyncio
async def test_empty_scan_writes_one_heartbeat_per_route_account(challenger_env):
    SessionLocal = challenger_env
    now = datetime(2026, 9, 8, 10, 2, 0)
    async with SessionLocal() as session:
        first = await run_strategy_iteration_challenger_accounts(session, now=now)
        await run_strategy_iteration_challenger_accounts(session, now=now)
        rows = list((await session.scalars(select(PaperAutoTradeLog).where(
            PaperAutoTradeLog.action == "scan"
        ))).all())

    assert first["events_considered"] == 0
    assert len(rows) == len(paper.PAPER_CHALLENGER_ACCOUNT_BY_ROUTE)
    assert len({row.account_id for row in rows}) == len(rows)
    assert all(row.strategy_version for row in rows)


@pytest.mark.asyncio
async def test_continuous_experiment_does_not_consume_prestart_event(
    challenger_env, monkeypatch,
):
    SessionLocal = challenger_env
    event_at = datetime(2026, 9, 7, 10, 0, 0)
    scan_at = datetime(2026, 9, 8, 10, 0, 0)
    monkeypatch.setattr(settings, "PAPER_CONTINUOUS_EXPERIMENT_ENABLED", True)
    monkeypatch.setattr(settings, "PAPER_EXPERIMENT_START_DATE", "2026-09-08")
    monkeypatch.setattr(settings, "PAPER_EXPERIMENT_ACTIVATION_AT", "2026-09-08T00:00:00")
    async with SessionLocal() as session:
        await _seed_confirmed(session, code="600203", route_id=ROUTE_B, now=event_at)
        result = await run_strategy_iteration_challenger_accounts(session, now=scan_at)

    assert result["events_considered"] == 0
    assert result["entries"] == 0


@pytest.mark.asyncio
async def test_continuous_experiment_does_not_consume_same_day_preactivation_event(
    challenger_env, monkeypatch,
):
    SessionLocal = challenger_env
    monkeypatch.setattr(settings, "PAPER_CONTINUOUS_EXPERIMENT_ENABLED", True)
    monkeypatch.setattr(settings, "PAPER_EXPERIMENT_START_DATE", "2026-09-07")
    monkeypatch.setattr(settings, "PAPER_EXPERIMENT_ACTIVATION_AT", "2026-09-07T14:33:17")
    async with SessionLocal() as session:
        await _seed_confirmed(
            session,
            code="600205",
            route_id=ROUTE_B,
            now=datetime(2026, 9, 7, 14, 33, 16),
        )
        result = await run_strategy_iteration_challenger_accounts(
            session,
            now=datetime(2026, 9, 7, 14, 34, 0),
        )

    assert result["events_considered"] == 0
    assert result["entries"] == 0


@pytest.mark.asyncio
async def test_comparison_marks_a2_and_e2_as_executing_not_control_only(challenger_env):
    SessionLocal = challenger_env
    async with SessionLocal() as session:
        payload = await build_strategy_iteration_challenger_comparison(session)

    coverage = {item["strategy_code"]: item for item in payload["strategy_coverage"]}
    assert coverage["A"]["control_sample_only"] is False
    assert coverage["A"]["challenger"]["account_name"] == paper.PAPER_ACCOUNT_CHALLENGER_A
    assert coverage["E"]["control_sample_only"] is False
    assert coverage["E"]["implementation_status"] == "independent_execution"
    assert coverage["E"]["challenger"]["account_name"] == paper.PAPER_ACCOUNT_CHALLENGER_E
    assert coverage["E"]["routes"][0]["execution_mode"] == "independent_paper_loop"


@pytest.mark.asyncio
async def test_submitted_order_is_idempotent_when_audit_log_is_missing(
    challenger_env, monkeypatch,
):
    SessionLocal = challenger_env
    now = datetime(2026, 9, 8, 10, 3, 0)

    async def pass_risk(*_args, **_kwargs):
        return {"final_level": "pass", "block_reasons": [], "warnings": []}

    monkeypatch.setattr(paper, "_risk_check_for_buy", pass_risk)
    async with SessionLocal() as session:
        event = await _seed_confirmed(
            session, code="600204", route_id=ROUTE_B, now=now
        )
        signal_token = _signal_token(event.event_key, event.route_id)
        session.add(TradeOrder(
            order_id="test-submitted-without-log",
            broker="paper",
            account_id=paper.PAPER_ACCOUNT_CHALLENGER_B,
            code=event.code,
            side="buy",
            order_type="limit",
            price=10.06,
            quantity=100,
            status="submitted",
            strategy_id="paper-challenger-forward",
            strategy_version=paper._strategy_version(
                paper.PAPER_ACCOUNT_CHALLENGER_B
            ),
            signal_id=signal_token,
            source=event.route_id,
            idempotency_key="test-submitted-without-log",
            decision_round_id="round-before-crash",
            trade_date=now.date(),
            created_at=now,
        ))
        await session.commit()

        result = await run_strategy_iteration_challenger_accounts(session, now=now)
        account = await paper._get_or_create_account(
            session, paper.PAPER_ACCOUNT_CHALLENGER_B
        )
        event_log_count = await session.scalar(
            select(func.count(PaperAutoTradeLog.id)).where(
                PaperAutoTradeLog.account_id == account.id,
                PaperAutoTradeLog.code == event.code,
            )
        )
        trade_count = await session.scalar(
            select(func.count(PaperTradeLog.id)).where(
                PaperTradeLog.account_id == account.id,
                PaperTradeLog.code == event.code,
            )
        )

    assert result["entries"] == 0
    assert event_log_count == 0
    assert trade_count == 0
