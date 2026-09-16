from datetime import datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.v1 import paper
from app.config.settings import settings
from app.data.quote_round import QuoteRoundArchive, build_quote_round_record
from app.db.session import Base
from app.models import paper as paper_models  # noqa: F401
from app.models import stock as stock_models  # noqa: F401
from app.models import trading as trading_models  # noqa: F401
from app.models.paper import PaperControlSample, PaperDailyOutcome, PaperPosition, PaperTradeLog
from app.models.trading import TradeFill, TradeOrder
from app.trading import service as trading_service
from paper_pending_fixture import accepted_frame


@pytest_asyncio.fixture
async def quote_execution_env(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "PAPER_DEFER_AUTO_FILL_TO_NEXT_ROUND", True)
    monkeypatch.setattr(settings, "PAPER_DEPTH_MAX_PARTICIPATION_RATIO", 1.0)
    monkeypatch.setattr(settings, "PAPER_MIN_COMMISSION", 5.0)
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'quote-execution.db'}",
        future=True,
    )
    SessionLocal = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield SessionLocal
    await engine.dispose()


def _round_payload(round_id: str, observed_at: datetime, records: list[dict]) -> dict:
    owned = []
    for record in records:
        owned.append({
            "name": record.get("name", "轮次测试"),
            "prev_close": record.get("prev_close", 9.8),
            "open": record.get("open", 9.9),
            "high": record.get("high", 10.2),
            "low": record.get("low", 9.8),
            "limit_up": record.get("limit_up", 10.78),
            "limit_down": record.get("limit_down", 8.82),
            "source_quote_at": observed_at,
            "received_at": observed_at,
            "updated_at": observed_at,
            "quote_round_id": round_id,
            **record,
        })
    return {
        "round_id": round_id,
        "trade_date": observed_at.date(),
        "quality_status": "ok",
        "quality_reason": "",
        "committed_at": observed_at,
        "as_of_at": observed_at,
        "config_version": "test-config-v1",
        "code_version": "test-code-v1",
        "records": owned,
        "records_by_code": {str(item["code"]): item for item in owned},
    }


@pytest.mark.asyncio
async def test_deferred_order_uses_next_round_depth_partial_fill_and_is_idempotent(
    quote_execution_env,
    monkeypatch,
):
    SessionLocal = quote_execution_env

    async def pass_risk(*_args, **_kwargs):
        return {"final_level": "pass", "block_reasons": [], "warnings": []}

    monkeypatch.setattr(trading_service, "_pre_trade_risk_check", pass_risk)
    # Isolate depth/accounting here; real TTL/route checks have dedicated integration tests.
    monkeypatch.setattr(trading_service, "_requires_pending_buy_validity", lambda _order: False)
    decision_at = datetime(2026, 9, 3, 10, 0, 0)
    decision_payload = _round_payload(
        "qr-decision",
        decision_at,
        [{"code": "600001", "price": 10.0, "ask1_price": 10.0, "ask1_volume": 10}],
    )

    async with SessionLocal() as session:
        token = paper._QUOTE_ROUND_CONTEXT.set(decision_payload)
        try:
            command = trading_service.SubmitOrderCommand(
                code="600001",
                side="buy",
                price=10.10,
                quantity=300,
                broker="paper",
                account_id=paper.PAPER_ACCOUNT_DEFAULT,
                strategy_id="paper-auto-short",
                source="next_day_plan",
                reason="验证下一轮五档撮合",
                idempotency_key="qr-decision:default:buy:600001",
                defer_until_next_round=True,
                deferred_metadata={"block_warn": True},
            )
            submitted = await trading_service.submit_order(session, command)
            replay = await trading_service.submit_order(session, command)
        finally:
            paper._QUOTE_ROUND_CONTEXT.reset(token)

        assert submitted["order"]["status"] == "submitted"
        assert submitted["fills"] == []
        assert replay["idempotent_replay"] is True
        assert replay["order"]["order_id"] == submitted["order"]["order_id"]
        assert await session.scalar(select(func.count(TradeOrder.id))) == 1
        assert await session.scalar(select(func.count(PaperPosition.id))) == 0

        partial_at = decision_at + timedelta(seconds=30)
        partial_payload = _round_payload(
            "qr-fill-1",
            partial_at,
            [{
                "code": "600001",
                "price": 10.0,
                "ask1_price": 10.0,
                "ask1_volume": 1,
                "ask2_price": 10.20,
                "ask2_volume": 50,
            }],
        )
        await accepted_frame(session, partial_payload)
        monkeypatch.setattr(paper, "_public_order_clock", lambda: partial_at)
        token = paper._QUOTE_ROUND_CONTEXT.set(partial_payload)
        try:
            partial = await trading_service.reconcile_paper_deferred_orders(
                session,
                account_id=paper.PAPER_ACCOUNT_DEFAULT,
                round_id="qr-fill-1",
                now=partial_at,
            )
            same_round_replay = await trading_service.reconcile_paper_deferred_orders(
                session,
                account_id=paper.PAPER_ACCOUNT_DEFAULT,
                round_id="qr-fill-1",
                now=partial_at,
            )
            first_trade = await session.scalar(
                select(PaperTradeLog).where(PaperTradeLog.trade_type == "buy")
            )
            duplicate = await paper.paper_buy(
                paper.SimBuyRequest(
                    code="600001",
                    price=10.0,
                    amount=100,
                    signal_id=str(first_trade.signal_id),
                ),
                account_name=paper.PAPER_ACCOUNT_DEFAULT,
                db=session,
            )
        finally:
            paper._QUOTE_ROUND_CONTEXT.reset(token)

        assert partial[0]["event"] == "partial"
        assert partial[0]["fills"][0]["quantity"] == 100
        assert partial[0]["fills"][0]["decision_round_id"] == "qr-decision"
        assert partial[0]["fills"][0]["fill_round_id"] == "qr-fill-1"
        assert same_round_replay == []
        assert duplicate["idempotent_replay"] is True
        position = await session.scalar(select(PaperPosition).where(PaperPosition.code == "600001"))
        assert position.buy_amount == 100

        final_at = partial_at + timedelta(seconds=30)
        final_payload = _round_payload(
            "qr-fill-2",
            final_at,
            [{
                "code": "600001",
                "price": 10.05,
                "ask1_price": 10.05,
                "ask1_volume": 2,
            }],
        )
        await accepted_frame(session, final_payload)
        monkeypatch.setattr(paper, "_public_order_clock", lambda: final_at)
        token = paper._QUOTE_ROUND_CONTEXT.set(final_payload)
        try:
            completed = await trading_service.reconcile_paper_deferred_orders(
                session,
                account_id=paper.PAPER_ACCOUNT_DEFAULT,
                round_id="qr-fill-2",
                now=final_at,
            )
        finally:
            paper._QUOTE_ROUND_CONTEXT.reset(token)

        assert completed[0]["event"] == "filled"
        assert completed[0]["order"]["filled_quantity"] == 300
        position = await session.scalar(select(PaperPosition).where(PaperPosition.code == "600001"))
        assert position.buy_amount == 300
        assert await session.scalar(select(func.count(PaperTradeLog.id))) == 2
        assert await session.scalar(select(func.count(TradeFill.id))) == 2
        fill_rounds = list(
            (
                await session.scalars(
                    select(TradeFill.fill_round_id).order_by(TradeFill.id)
                )
            ).all()
        )
        assert fill_rounds == ["qr-fill-1", "qr-fill-2"]


@pytest.mark.asyncio
async def test_a2_e2_control_samples_are_isolated_and_daily_outcomes_are_terminal(
    quote_execution_env, monkeypatch,
):
    monkeypatch.setattr(settings, "PAPER_CONTINUOUS_EXPERIMENT_ENABLED", False)
    SessionLocal = quote_execution_env
    observed_at = datetime(2026, 9, 3, 10, 0, 0)
    first_payload = _round_payload(
        "qr-control-1",
        observed_at,
        [{"code": "600002", "price": 10.0, "ask1_price": 10.0, "ask1_volume": 1}],
    )

    async with SessionLocal() as session:
        champion_a = await paper._get_or_create_account(session, paper.PAPER_ACCOUNT_DEFAULT)
        champion_b = await paper._get_or_create_account(session, paper.PAPER_ACCOUNT_PROMOTION)
        token = paper._QUOTE_ROUND_CONTEXT.set(first_payload)
        try:
            sample = await paper._record_control_sample(
                session,
                account=champion_a,
                trade_date=observed_at.date(),
                observed_at=observed_at,
                candidates=[{
                    "code": "600002",
                    "name": "控制样本",
                    "_source": "next_day_plan",
                    "total_score": 96.0,
                }],
            )
            unsupported = await paper._record_control_sample(
                session,
                account=champion_b,
                trade_date=observed_at.date(),
                observed_at=observed_at,
                candidates=[{"code": "600002", "total_score": 96.0}],
            )
            await session.commit()
        finally:
            paper._QUOTE_ROUND_CONTEXT.reset(token)

        assert sample.price == 10.0
        assert sample.quote_round_id == "qr-control-1"
        assert sample.excluded_from_performance is True
        assert sample.forced_probe is False
        assert unsupported is None
        challenger_a = await paper._get_or_create_account(
            session,
            paper.PAPER_ACCOUNT_CHALLENGER_A,
        )
        assert sample.challenger_account_id == challenger_a.id

        next_at = observed_at + timedelta(seconds=30)
        next_payload = _round_payload(
            "qr-control-2",
            next_at,
            [{"code": "600002", "price": 10.1, "ask1_price": 10.0, "ask1_volume": 1}],
        )
        token = paper._QUOTE_ROUND_CONTEXT.set(next_payload)
        try:
            updated = await paper._update_control_sample_next_round(
                session,
                account=champion_a,
                trade_date=observed_at.date(),
                observed_at=next_at,
            )
            finalized = await paper.finalize_paper_daily_outcomes(
                session,
                trade_date=observed_at.date(),
                observed_at=observed_at.replace(hour=15, minute=45),
            )
        finally:
            paper._QUOTE_ROUND_CONTEXT.reset(token)

        await session.refresh(sample)
        assert updated == 1
        assert sample.next_round_id == "qr-control-2"
        assert sample.next_round_fillable_amount == 100
        assert sample.close_price == 10.1
        assert len(finalized["accounts"]) == 12
        outcomes = list((await session.scalars(select(PaperDailyOutcome))).all())
        assert len(outcomes) == 12
        outcome_by_account_id = {item.account_id: item for item in outcomes}
        assert outcome_by_account_id[challenger_a.id].terminal_status == "control_only"
        assert outcome_by_account_id[challenger_a.id].is_terminal is True
        assert await session.scalar(select(func.count(PaperControlSample.id))) == 2


def test_quote_round_archive_is_zstd_parquet_and_restores_exact_batches(tmp_path):
    committed_at = datetime(2026, 9, 3, 10, 0, 30)
    records = [
        {
            "code": "600001",
            "name": "归档一",
            "price": 10.0,
            "prev_close": 9.8,
            "open": 9.9,
            "high": 10.1,
            "low": 9.8,
            "change_pct": 2.04,
            "volume": 1000,
            "amount": 1_000_000,
            "source_quote_at": committed_at - timedelta(seconds=2),
            "received_at": committed_at - timedelta(seconds=1),
        },
        {
            "code": "600002",
            "name": "归档二",
            "price": 20.0,
            "prev_close": 20.0,
            "open": 20.0,
            "high": 20.1,
            "low": 19.9,
            "change_pct": 0.0,
            "volume": 2000,
            "amount": 4_000_000,
            "source_quote_at": committed_at - timedelta(seconds=1),
            "received_at": committed_at,
        },
    ]
    round_record = build_quote_round_record(
        records,
        expected_count=2,
        committed_at=committed_at,
        component_watermarks={"fund_flow_observed_at": committed_at},
    )
    archive = QuoteRoundArchive(tmp_path / "quote-rounds")
    result = archive.write_round(
        round_record,
        records,
        focus_codes={"600001"},
    )

    assert round_record["quality_status"] == "ok"
    assert round_record["as_of_at"] == committed_at - timedelta(seconds=1)
    assert Path(result["archive_path"]).exists()
    assert Path(result["focus_path"]).exists()
    assert Path(result["minute_archive_path"]).exists()
    restored = archive.restore_recent_batches(committed_at.date(), minutes=8)
    assert len(restored) == 1
    assert {item["code"] for item in restored[0]} == {"600001", "600002"}
    assert {item["quote_round_id"] for item in restored[0]} == {
        round_record["round_id"]
    }
