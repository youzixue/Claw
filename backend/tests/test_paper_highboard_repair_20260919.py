"""高标入口实验和D2证据缺口：隔离数据库，禁止回填真实交易。"""
import json
from datetime import date, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.api.v1 import paper
from app.models.paper import PaperAutoTradeLog, PaperDailyOutcome, PaperShadowEvent
from app.models.stock import AuctionData, LimitUpPool, StockSpot
from app.models.trading import TradeOrder, TradeFill
from app.paper import experiment, strategy_iteration_shadow as shadow
from app.paper.account_policy import ACCOUNT_NAMES
from auction_test_evidence import verified_auction_fields
from test_paper_api import paper_client
from test_strategy_iteration_shadow import shadow_env, _seed_structures, _quote


@pytest.mark.asyncio
@pytest.mark.parametrize("seal,breaks,reason", [
    (50_000_000, 2, None), (49_999_999, 2, "seal_amount_below_min"),
    (71_875_482, 5, "break_count_above_max"),
    (None, 0, "seal_amount_missing"), (-1, 0, "seal_amount_missing"),
    (60_000_000, None, "break_count_missing"),
    (60_000_000, -1, "break_count_missing"),
])
async def test_e2_seal_experiment_preserves_break_gate_and_data_unknown(
    paper_client, monkeypatch, seal, breaks, reason,
):
    _, maker = paper_client
    day = date(2026, 9, 18)
    previous = date(2026, 9, 17)
    monkeypatch.setattr(paper.trade_calendar, "previous_trade_day", AsyncMock(return_value=previous))
    monkeypatch.setattr(paper.settings, "PAPER_HIGHBOARD_MIN_SEAL_AMOUNT", 1.0)
    monkeypatch.setattr(paper.settings, "PAPER_CHALLENGER_E_MIN_SEAL_AMOUNT", .5)
    async with maker() as db:
        item = LimitUpPool(code="600123", name="高标样本", trade_date=previous,
                          consecutive_days=4, seal_amount=seal, break_count=0, quarantined=False)
        db.add_all([item, StockSpot(code="600123", price=10.1, prev_close=10, open=10,
                                   low=10, high=10.12, avg_price=10.05, change_pct=1, limit_up=11, limit_down=9)])
        await db.flush()
        item.break_count = breaks  # 覆盖SQLAlchemy插入默认值，真实测试NULL。
        await db.flush()
        diagnostics = []
        rows, _ = await paper._tenbagger_midline_candidates(
            db, limit=10, trade_date=day, account_name="challenger_e", diagnostics=diagnostics)
        main, _ = await paper._tenbagger_midline_candidates(db, limit=10, trade_date=day)
        assert not main
        if reason is None:
            assert len(rows) == 1
            assert rows[0]["quality_checks"]["min_seal_amount_yuan"] == 50_000_000
        else:
            assert not rows
            assert diagnostics[0]["reason_code"] == reason
            assert diagnostics[0]["stage_code"] == ("data_gate" if reason.endswith("missing") else "strategy_filter")


@pytest.mark.asyncio
async def test_e2_reports_both_quality_failures(paper_client, monkeypatch):
    _, maker = paper_client
    previous = date(2026, 9, 17)
    monkeypatch.setattr(paper.trade_calendar, "previous_trade_day", AsyncMock(return_value=previous))
    async with maker() as db:
        db.add(LimitUpPool(code="600123", trade_date=previous, consecutive_days=5,
                          seal_amount=28_054_066, break_count=23, quarantined=False))
        await db.flush()
        diagnostics = []
        rows, _ = await paper._tenbagger_midline_candidates(db, limit=10,
            trade_date=date(2026, 9, 18), account_name="challenger_e", diagnostics=diagnostics)
        assert not rows
        checks = diagnostics[0]["candidate"]["quality_checks"]
        assert checks["seal_amount_passed"] is False
        assert checks["break_count_passed"] is False


def test_e2_threshold_version_does_not_change_other_accounts(monkeypatch):
    before = {name: paper._strategy_version(name) for name in ACCOUNT_NAMES}
    monkeypatch.setattr(paper.settings, "PAPER_CHALLENGER_E_MIN_SEAL_AMOUNT", 1.0)
    after = {name: paper._strategy_version(name) for name in ACCOUNT_NAMES}
    assert {name for name in before if before[name] != after[name]} == {"challenger_e"}


@pytest.mark.asyncio
@pytest.mark.parametrize("contaminant", ["unverified_early", "unverified_final", "source_clock", "future"])
async def test_d2_verified_path_survives_unrelated_rows(shadow_env, monkeypatch, contaminant):
    monkeypatch.setattr(paper.settings, "PAPER_STRATEGY_ITERATION_MIN_QUOTE_COVERAGE", .2)
    day = date(2026, 9, 1)
    at = datetime(2026, 9, 1, 9, 40)
    async with shadow_env() as db:
        await _seed_structures(db)
        hhmmss = "09:17:00" if contaminant == "unverified_early" else "09:25:20"
        fields = {}
        if contaminant in {"source_clock", "future"}:
            fields = verified_auction_fields(day, hhmmss)
            if contaminant == "source_clock":
                # 晚接收，但源时钟早于真正最新的09:25:00最终段。
                fields["source_quote_at"] = datetime(2026, 9, 1, 9, 25)
                latest = await db.scalar(select(AuctionData).where(
                    AuctionData.code == "600004", AuctionData.auction_time == "09:25:00"))
                latest.auction_time = "09:25:10"
                for key, value in verified_auction_fields(day, "09:25:10").items():
                    setattr(latest, key, value)
            else:
                fields["received_at"] = at + timedelta(seconds=1)
                fields["observed_at"] = at + timedelta(seconds=1)
        db.add(AuctionData(code="600004", trade_date=day, auction_time=hhmmss,
                           auction_price=9.0, prev_close=10, auction_volume=1000,
                           auction_amount=9000, **fields))
        await db.commit()
        await shadow.scan_strategy_iteration_shadow(db, [_quote("600004")], at)
        events = list((await db.scalars(select(PaperShadowEvent).where(
            PaperShadowEvent.route_id == shadow.ROUTE_D))).all())
        eligible = next(item for item in events if item.event_type == "eligible")
        prior = json.loads(eligible.snapshot_json)["prior_structure"]
        assert prior["auction_volume_path_verified"] is True
        assert prior["baseline_evidence_status"] == prior["final_evidence_status"] == "ok"
        assert prior["final_change_pct"] == pytest.approx(.5)
        assert all(item.event_type not in {"coverage_blocked", "evidence_blocked"} for item in events)


@pytest.mark.asyncio
@pytest.mark.parametrize("scope,expected", [
    ("current", "data_limited"), ("old_version", "no_candidate"),
    ("other_route", "no_candidate"), ("future_observed", "no_candidate"),
    ("future_created", "no_candidate"), ("structure", "candidate_observed"),
])
async def test_daily_outcome_includes_only_visible_bound_shadow_evidence(
    paper_client, monkeypatch, scope, expected,
):
    _, maker = paper_client
    at = datetime(2026, 9, 18, 15, 45)
    monkeypatch.setattr(paper.settings, "PAPER_CONTINUOUS_EXPERIMENT_ENABLED", True)
    monkeypatch.setattr(paper, "_record_control_sample", AsyncMock(return_value=None))
    async with maker() as db:
        account = await paper._get_or_create_account(db, "challenger_d")
        version = paper._strategy_version("challenger_d")
        identity = experiment.execution_signal_identity("challenger_d")
        db.add(PaperAutoTradeLog(account_id=account.id, trade_date=at.date(),
            created_at=at.replace(hour=10), run_id="scan", action="scan", decision="wait",
            source="system", reason="scan", strategy_version=version))
        db.add(PaperShadowEvent(event_key="current-evidence", code="MARKET", trade_date=at.date(),
            route_id="d_other" if scope == "other_route" else identity["route_id"],
            route_version="old" if scope == "old_version" else identity["route_version"],
            event_type="structural_pool" if scope == "structure" else "coverage_blocked",
            status="observed", snapshot_json="{}",
            observed_at=at + timedelta(seconds=1) if scope == "future_observed" else at,
            created_at=at + timedelta(seconds=1) if scope == "future_created" else at))
        await db.flush()
        await paper.finalize_paper_daily_outcomes(db, trade_date=at.date(), observed_at=at)
        outcome = await db.scalar(select(PaperDailyOutcome).where(PaperDailyOutcome.account_id == account.id))
        assert outcome.terminal_status == expected
        assert outcome.submitted_order_count == outcome.fill_count == 0
        detail = json.loads(outcome.details_json)["shadow_evidence"]
        assert detail["data_wait_record_count"] == (1 if scope == "current" else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("filled_quantity,expected", [(0, "order_unfilled"), (100, "partial_fill"), (200, "filled")])
async def test_daily_data_gap_does_not_override_real_order_result(paper_client, monkeypatch, filled_quantity, expected):
    _, maker = paper_client
    at = datetime(2026, 9, 18, 15, 45)
    monkeypatch.setattr(paper.settings, "PAPER_CONTINUOUS_EXPERIMENT_ENABLED", True)
    monkeypatch.setattr(paper, "_record_control_sample", AsyncMock(return_value=None))
    async with maker() as db:
        account = await paper._get_or_create_account(db, "challenger_d")
        identity = experiment.execution_signal_identity("challenger_d")
        db.add(PaperShadowEvent(event_key="gap", code="MARKET", trade_date=at.date(),
            route_id=identity["route_id"], route_version=identity["route_version"],
            event_type="coverage_blocked", status="coverage_blocked", observed_at=at, created_at=at))
        db.add(TradeOrder(order_id="test-order", broker="paper", account_id="challenger_d",
            code="600123", side="buy", order_type="limit", quantity=200, price=10,
            filled_quantity=filled_quantity, status="filled" if filled_quantity == 200 else "submitted",
            strategy_version=paper._strategy_version("challenger_d"), trade_date=at.date(), created_at=at))
        if filled_quantity:
            db.add(TradeFill(fill_id="test-fill", order_id="test-order", code="600123", side="buy",
                price=10, quantity=filled_quantity, trade_date=at.date(), filled_at=at))
        await db.flush()
        await paper.finalize_paper_daily_outcomes(db, trade_date=at.date(), observed_at=at)
        result = await db.scalar(select(PaperDailyOutcome).where(PaperDailyOutcome.account_id == account.id))
        assert result.terminal_status == expected
        assert result.submitted_order_count == 1
        assert result.fill_count == int(filled_quantity > 0)
        assert json.loads(result.details_json)["shadow_evidence"]["data_wait_record_count"] == 1
