"""前向模拟实验协议：不以成交数量作为成功指标，不触碰真实业务数据库。"""
from datetime import datetime, date
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pandas as pd
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.v1 import paper
from app.config.settings import settings
from app.db.session import Base
from app.data.quote_round import QuoteRoundArchive, build_quote_round_record
from app.models.stock import LimitUpPool, StockSpot
from app.paper.experiment import EXPERIMENT_ACCOUNTS, experiment_active, execution_version
from app.risk.engine import RiskContext, RiskLevel
from app.risk.rules import MaxDrawdownRule, SentimentCircuitBreakerRule, BlacklistRule, PositionLimitRule
from app.trading import service
from app.trading.broker import PaperBrokerAdapter, BrokerOrderRequest
from paper_pending_fixture import accepted_frame


@pytest.fixture(autouse=True)
def protocol(monkeypatch):
    monkeypatch.setattr(settings, "PAPER_CONTINUOUS_EXPERIMENT_ENABLED", True)
    monkeypatch.setattr(settings, "PAPER_EXPERIMENT_START_DATE", "2026-09-08")
    monkeypatch.setattr(settings, "PAPER_EXPERIMENT_ACTIVATION_AT", "2026-09-08T00:00:00")


@pytest_asyncio.fixture
async def db(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'experiment.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


def test_all_accounts_override_old_pauses_but_not_start_boundary(monkeypatch):
    for key in (
        "PAPER_PROMOTION_AUTO_ORDER_ENABLED", "PAPER_MAINLINE_AUTO_ORDER_ENABLED",
        "PAPER_AUCTION_AUTO_ORDER_ENABLED", "PAPER_TENBAGGER_AUTO_ORDER_ENABLED",
        "PAPER_REVERSAL_AUTO_ORDER_ENABLED", "PAPER_CHALLENGER_A_AUTO_ORDER_ENABLED",
        "PAPER_CHALLENGER_B_AUTO_ORDER_ENABLED", "PAPER_CHALLENGER_C_AUTO_ORDER_ENABLED",
        "PAPER_CHALLENGER_D_AUTO_ORDER_ENABLED", "PAPER_CHALLENGER_E_AUTO_ORDER_ENABLED",
        "PAPER_CHALLENGER_F2_AUTO_ORDER_ENABLED",
    ):
        monkeypatch.setattr(settings, key, False)
    assert len(EXPERIMENT_ACCOUNTS) == 12
    assert set(paper.PAPER_SCAN_ACCOUNTS) == {*paper.PAPER_ALL_ACCOUNTS, "challenger_e"}
    assert paper.PAPER_CHALLENGER_ACCOUNT_BY_ROUTE["momentum_first_retest"] == "challenger_a"
    for name in EXPERIMENT_ACCOUNTS:
        assert not paper._strategy_auto_order_enabled(name, now=datetime(2026, 9, 7, 14))
        assert paper._strategy_auto_order_enabled(name, now=datetime(2026, 9, 8, 10))
        assert not experiment_active(name, broker="qmt", at=date(2026, 9, 8))
    assert not experiment_active("unknown", at=date(2026, 9, 8))
    monkeypatch.setattr(settings, "PAPER_EXPERIMENT_START_DATE", "invalid")
    assert not experiment_active("default", at=date(2026, 9, 8))


def test_intraday_activation_boundary_is_forward_only(monkeypatch):
    monkeypatch.setattr(settings, "PAPER_EXPERIMENT_START_DATE", "2026-09-07")
    monkeypatch.setattr(settings, "PAPER_EXPERIMENT_ACTIVATION_AT", "2026-09-07T14:33:17")
    assert not experiment_active("default", at=datetime(2026, 9, 7, 14, 33, 16))
    assert experiment_active("default", at=datetime(2026, 9, 7, 14, 33, 17))
    assert experiment_active("default", at=datetime(2026, 9, 8, 9, 30))
    monkeypatch.setattr(settings, "PAPER_EXPERIMENT_ACTIVATION_AT", "invalid")
    assert not experiment_active("default", at=datetime(2026, 9, 7, 15))


@pytest.mark.parametrize("phase", ["freezing", "recovery", "divergence", "climax"])
def test_market_phase_is_label_not_stop_when_required_data_valid(phase):
    ctx = RiskContext(action="buy", sentiment_cycle=phase, sentiment_quality_status="ok",
                      is_paper_experiment=True, sentiment_required=True, max_drawdown=48)
    assert SentimentCircuitBreakerRule().check(ctx).level == RiskLevel.PASS
    decision = MaxDrawdownRule().check(ctx)
    assert decision.level == RiskLevel.PASS
    assert decision.detail["current_drawdown"] == 48
    assert ctx.max_drawdown == 48


@pytest.mark.parametrize("quality", ["missing", "stale", "degraded", "unchecked"])
def test_only_optional_macro_data_can_be_missing(quality):
    ctx = RiskContext(action="buy", is_paper_experiment=True, sentiment_quality_status=quality)
    assert SentimentCircuitBreakerRule().check(ctx).level == RiskLevel.BLOCK
    ctx.sentiment_required = False
    assert SentimentCircuitBreakerRule().check(ctx).level == RiskLevel.PASS
    ctx.is_paper_experiment = False
    if quality != "unchecked":
        assert SentimentCircuitBreakerRule().check(ctx).level == RiskLevel.BLOCK


def test_sentiment_snapshot_clock_rejects_missing_future_and_stale():
    from datetime import timedelta
    from app.paper.experiment import sentiment_quality_at
    at = datetime(2026, 9, 8, 10)
    state = SimpleNamespace(quality_status="ok", quality_reason="", observed_at=None)
    assert sentiment_quality_at(state, at=at, strict=True)[0] == "missing"
    for observed in (at + timedelta(seconds=1), at - timedelta(seconds=601)):
        state.observed_at = observed
        assert sentiment_quality_at(state, at=at, strict=True)[0] == "stale"
    state.observed_at = at - timedelta(seconds=60)
    assert sentiment_quality_at(state, at=at, strict=True)[0] == "ok"


def test_blacklist_and_position_limits_remain_hard():
    ctx = RiskContext(action="buy", is_paper_experiment=True, is_st=True, code="600001")
    assert BlacklistRule().check(ctx).level == RiskLevel.BLOCK
    ctx.is_st = False
    ctx.total_assets = 50000
    ctx.cash = 50000
    ctx.price = 10
    ctx.amount = 5000
    assert PositionLimitRule().check(ctx).level == RiskLevel.BLOCK


def test_version_is_distinct_bounded_and_protocol_sensitive(monkeypatch):
    versions = [paper._strategy_version(name) for name in EXPERIMENT_ACCOUNTS]
    assert len(set(versions)) == 12
    assert all(len(value) <= 64 for value in versions)
    old = execution_version("same-base")
    monkeypatch.setattr(settings, "PAPER_EXPERIMENT_VERSION", "continuous_paper_v2")
    assert execution_version("same-base") != old


@pytest.mark.asyncio
async def test_e2_accepts_strong_entry_without_relaxing_yesterday_seal(db, monkeypatch):
    day = date(2026, 9, 8)
    monkeypatch.setattr(paper.trade_calendar, "previous_trade_day", AsyncMock(return_value=date(2026, 9, 7)))
    db.add(LimitUpPool(code="600001", name="测试", trade_date=date(2026, 9, 7),
                       consecutive_days=5, seal_amount=200_000_000, break_count=1, quarantined=False))
    db.add(StockSpot(code="600001", name="测试", price=10.8, prev_close=10, open=10.5,
                     high=10.8, low=10.4, avg_price=10.6, change_pct=8, limit_up=11,
                     ask1_price=10.81, ask1_volume=100))
    await db.flush()
    primary, _ = await paper._tenbagger_midline_candidates(db, limit=10, trade_date=day)
    secondary, _ = await paper._tenbagger_midline_candidates(
        db, limit=10, trade_date=day, account_name="challenger_e")
    assert primary == []
    assert [item["code"] for item in secondary] == ["600001"]
    assert secondary[0]["entry_variant"] == "e2_strong_reseal"
    assert secondary[0]["stop_loss_pct"] == settings.PAPER_HIGHBOARD_STOP_LOSS_PCT
    row = await db.get(LimitUpPool, 1)
    row.seal_amount = 79_146_374
    await db.flush()
    assert not (await paper._tenbagger_midline_candidates(
        db, limit=10, trade_date=day, account_name="challenger_e"))[0]


@pytest.mark.asyncio
async def test_e2_internal_broker_buy_and_secondary_exit_authorization(monkeypatch):
    seen = []
    async def execute(_request, *, account_name, db):
        seen.append((account_name, paper._CHALLENGER_INTERNAL_ORDER_CONTEXT.get()))
        return {"trade": {"id": 1, "price": 10, "amount": 100}}
    monkeypatch.setattr(paper, "_book_paper_buy", execute)
    monkeypatch.setattr(paper, "_book_paper_sell", execute)
    from app.trading.service import _dispatch_broker_order
    adapter = PaperBrokerAdapter()
    await _dispatch_broker_order(adapter, None, BrokerOrderRequest(
        order_id="test", code="600001", side="buy", price=10, quantity=100,
        account_name="challenger_e", strategy_id="paper-auto-short",
        source="tenbagger_midline", signal_id="auto-tenbagger_midline-qr-600001"))
    for name in paper.PAPER_CHALLENGER_ACCOUNTS:
        await _dispatch_broker_order(adapter, None, BrokerOrderRequest(
            order_id="test", code="600001", side="sell", price=10, quantity=100,
            account_name=name, strategy_id="paper-auto-short", source="position",
            signal_id="auto-sell-qr-600001"))
    assert all(authorized for _, authorized in seen)
    assert not paper._CHALLENGER_INTERNAL_ORDER_CONTEXT.get()


def test_all_stock_vwap_is_archived_without_inventing_missing_values(tmp_path):
    at = datetime(2026, 9, 8, 10)
    records = [
        {"code": code, "name": "测试", "price": 10, "avg_price": vwap,
         "updated_at": at, "source_quote_at": at, "received_at": at, "committed_at": at}
        for code, vwap in (("600001", 9.7), ("600002", None))
    ]
    record = build_quote_round_record(records, expected_count=2, committed_at=at)
    archive = QuoteRoundArchive(tmp_path)
    archive.write_round(record, records)
    files = list((tmp_path / "minute").rglob("*.parquet"))
    frame = pd.read_parquet(files[0]).set_index("code")
    assert frame.loc["600001", "avg_price"] == 9.7
    assert pd.isna(frame.loc["600002", "avg_price"])


@pytest.mark.asyncio
@pytest.mark.parametrize("name,source", [
    ("default", "next_day_plan"), ("promotion", "promotion_promotion"),
    ("mainline", "promotion_mainline"), ("auction", "promotion_auction"),
    ("tenbagger", "tenbagger_midline"), ("reversal", "reversal_pullback"),
    ("challenger_a", "momentum_first_retest"),
    ("challenger_b", "b_weak_open_second_board"),
    ("challenger_c", "c_recent_limit_relaunch"),
    ("challenger_d", "d_auction_recovery"),
    ("challenger_e", "tenbagger_midline"),
    ("challenger_f2", "f2_highboard_break_reclaim"),
])
async def test_each_of_twelve_accounts_can_fill_then_exit_on_next_day(db, monkeypatch, name, source):
    from datetime import timedelta
    from sqlalchemy import select
    from app.models.paper import PaperPosition, PaperTradeLog
    from app.paper.experiment_report import build_experiment_report

    # This is account/T+1 accounting isolation, not a twelve-route signal fixture.
    monkeypatch.setattr(service, "_requires_pending_buy_validity", lambda _order: False)
    monkeypatch.setattr(service, "_pre_trade_risk_check", AsyncMock(return_value={
        "final_level": "pass", "warnings": [], "block_reasons": [],
    }))
    monkeypatch.setattr(paper.trade_calendar, "previous_trade_day", AsyncMock(return_value=date(2026, 9, 7)))
    monkeypatch.setattr(paper.trade_calendar, "trade_days_between", AsyncMock(return_value=[date(2026, 9, 9)]))

    def context(round_id, at):
        row = {
            "code": "600001", "name": "测试", "price": 10, "prev_close": 10,
            "open": 10, "high": 10, "low": 10, "avg_price": 10,
            "limit_up": 11, "limit_down": 9, "ask1_price": 10,
            "ask1_volume": 10, "bid1_price": 10, "bid1_volume": 10,
            "updated_at": at, "source_quote_at": at, "received_at": at,
            "quote_round_id": round_id,
        }
        return {"round_id": round_id, "committed_at": at, "as_of_at": at,
                "trade_date": at.date(), "quality_status": "ok",
                "config_version": "fixture-config", "code_version": "fixture-code",
                "records": [row], "records_by_code": {"600001": row}}

    start = datetime(2026, 9, 8, 10)
    async def submit(side, at, round_id):
        token = paper._QUOTE_ROUND_CONTEXT.set(context(round_id, at))
        try:
            return await service.submit_order(db, service.SubmitOrderCommand(
                code="600001", side=side, price=10.1 if side == "buy" else 9.9,
                quantity=100, account_id=name,
                strategy_id=("paper-auto-short" if side == "sell" or name == "challenger_e" or name not in paper.PAPER_CHALLENGER_ACCOUNTS
                             else "paper-challenger-forward"),
                source="position" if side == "sell" else source,
                signal_id="auto-sell-test" if side == "sell" else
                          "auto-tenbagger_midline-test" if name == "challenger_e" else "chlg-test",
                decision_at=at, as_of_at=at, decision_round_id=round_id,
                defer_until_next_round=True, idempotency_key=f"{name}:{round_id}:{side}",
                deferred_metadata={"stop_loss_price": 9.4, "block_warn": side == "buy"},
            ))
        finally:
            paper._QUOTE_ROUND_CONTEXT.reset(token)

    async def reconcile(at, round_id):
        # Matching is valid even for the same-day exit: the real ledger must reject T+1.
        payload = context(round_id, at)
        await accepted_frame(db, payload)
        monkeypatch.setattr(paper, "_public_order_clock", lambda: at)
        token = paper._QUOTE_ROUND_CONTEXT.set(payload)
        try:
            return await service.reconcile_paper_deferred_orders(
                db, account_id=name, round_id=round_id, now=at)
        finally:
            paper._QUOTE_ROUND_CONTEXT.reset(token)

    order = await submit("buy", start, "buy-decision")
    assert order["order"]["status"] == "submitted"
    assert not order["fills"]
    fills = await reconcile(start + timedelta(seconds=30), "buy-fill")
    assert fills[0]["event"] == "filled", fills
    position = await db.scalar(select(PaperPosition))
    assert position.stop_loss_price == 9.4
    assert not await reconcile(start + timedelta(seconds=30), "buy-fill")

    # 同日尝试退出不得穿透T+1；所有账户使用同一真实落账路径。
    await submit("sell", start + timedelta(minutes=1), "same-day-sell")
    blocked = await reconcile(start + timedelta(seconds=90), "same-day-fill")
    assert not blocked[0]["fills"]
    assert "T+1" in blocked[0]["reason"], blocked
    await submit("sell", start + timedelta(days=1), "next-day-sell")
    sold = await reconcile(start + timedelta(days=1, seconds=30), "next-day-fill")
    assert sold[0]["event"] == "filled", sold
    assert position.is_closed
    rows = list((await db.scalars(select(PaperTradeLog))).all())
    assert len(rows) == 2
    assert rows[0].strategy_version == rows[1].strategy_version
    report = await build_experiment_report(db, account_name=name, now=start+timedelta(days=1, minutes=1))
    assert report["accounts"][0]["closed_round_trips"] == 1
    assert report["accounts"][0]["net_pnl"] < 0  # 同价买卖扣费后不是盈利


@pytest.mark.asyncio
@pytest.mark.parametrize("fill_blocked", [False, True])
async def test_e2_reseal_queue_uses_its_authorized_lifecycle_and_survives_risk_recheck(db, monkeypatch, fill_blocked):
    from datetime import timedelta
    from sqlalchemy import select
    from app.models.paper import PaperPosition
    # Lifecycle/risk regression; real E2 queue entry contracts have separate tests.
    monkeypatch.setattr(service, "_requires_pending_buy_validity", lambda _order: False)
    monkeypatch.setattr(paper.trade_calendar, "previous_trade_day", AsyncMock(return_value=date(2026, 9, 7)))
    monkeypatch.setattr(service, "_pre_trade_risk_check", AsyncMock(return_value={
        "final_level": "pass", "warnings": [], "block_reasons": [],
    }))
    at = datetime(2026, 9, 8, 10)
    row = StockSpot(code="600001", name="测试", price=11, prev_close=10, open=10.5,
        low=10.4, high=11, avg_price=10.7, limit_up=11, limit_down=9,
        bid1_price=11, bid1_volume=100, ask1_price=0, volume=50000, updated_at=at)
    db.add(row)
    await db.flush()
    order = await service.submit_order(db, service.SubmitOrderCommand(
        code="600001", side="buy", price=11, quantity=100, account_id="challenger_e",
        strategy_id="paper-auto-short", source="tenbagger_midline",
        signal_id="auto-tenbagger_midline-queue", decision_at=at,
        decision_round_id="queue-decision", as_of_at=at, queue_if_limit_up=True,
        queue_metadata={"stop_loss_price": 10.34},
    ))
    assert order["order"]["status"] == "submitted" and not order["fills"]
    row.volume = 50110
    row.updated_at = at + timedelta(seconds=30)
    await db.flush()
    if fill_blocked:
        monkeypatch.setattr(service, "_pre_trade_risk_check", AsyncMock(return_value={
            "final_level": "block", "warnings": [],
            "block_reasons": [{"message": "标的停牌"}],
        }))
    if not fill_blocked:
        # Explicit later Tencent frame; do not retimestamp the original queue decision.
        row.source_quote_at = row.received_at = row.updated_at
        row.quote_round_id = "queue-fill"
        payload = {"round_id": "queue-fill", "quality_status": "ok",
            "committed_at": row.updated_at, "as_of_at": row.source_quote_at,
            "config_version": "fixture-config", "code_version": "fixture-code",
            "records": [{key: getattr(row, key) for key in (
                "code", "name", "price", "prev_close", "open", "low", "high", "avg_price",
                "limit_up", "limit_down", "bid1_price", "bid1_volume", "ask1_price", "volume",
                "updated_at", "source_quote_at", "received_at", "quote_round_id")}]}
        await accepted_frame(db, payload)
        monkeypatch.setattr(paper, "_public_order_clock", lambda: at + timedelta(seconds=30))
    else:
        payload = {}
    token = paper._QUOTE_ROUND_CONTEXT.set(payload)
    try:
        result = await service.reconcile_paper_limit_up_orders(
            db, account_id="challenger_e", now=at + timedelta(seconds=30))
    finally:
        paper._QUOTE_ROUND_CONTEXT.reset(token)
    if fill_blocked:
        assert result[0]["event"] == "risk_blocked"
        assert not await db.scalar(select(PaperPosition))
    else:
        assert result[0]["fills"], result
        position = await db.scalar(select(PaperPosition))
        assert position.stop_loss_price == 10.34
