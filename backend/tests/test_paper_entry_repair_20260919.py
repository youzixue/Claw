"""候选衔接、统一入场锚与加仓边界；只使用pytest隔离库。"""
import json
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.api.v1 import paper
from app.config.settings import settings
from app.models.paper import PaperAutoTradeLog, PaperPosition, PaperTradeLog
from app.models.stock import StockSpot
from app.models.trading import TradeOrder
from app.paper.strategy_iteration_challenger import _today_buy_count
from test_paper_api import paper_client, _governed_promotion_run, _governed_promotion_snapshot

DAY = date(2026, 9, 18)
AT = datetime(2026, 9, 18, 10, 10)


@pytest.mark.asyncio
@pytest.mark.parametrize("latest_status,future,expected", [("completed", False, 1), ("blocked", False, 0), ("completed", True, 0)])
async def test_b_uses_new_visible_batch_after_failed_opening_without_fallback(paper_client, monkeypatch, latest_status, future, expected):
    _, maker = paper_client
    monkeypatch.setattr(paper.trade_calendar, "previous_trade_day", AsyncMock(return_value=DAY-timedelta(days=1)))
    async with maker() as db:
        opening = _governed_promotion_run(run_key="failed-open", reference_trade_date=DAY,
            snapshot_context="promotion_0935", as_of_at=AT.replace(hour=9, minute=35), status="blocked")
        latest = _governed_promotion_run(run_key="intraday", reference_trade_date=DAY,
            snapshot_context="promotion_1000", as_of_at=AT.replace(minute=0), status=latest_status)
        if future:
            latest.completed_at = AT + timedelta(seconds=1)
        db.add_all([opening, latest])
        await db.flush()
        db.add(_governed_promotion_snapshot(run_id=latest.id, record_key="live-b", code="600001",
            prediction_trade_date=DAY, probability=.5))
        db.add(StockSpot(code="600001", name="test", price=10.1, prev_close=10, change_pct=1,
                         limit_up=11, volume_ratio=1.5))
        await db.commit()
        rows, notes = await paper._promotion_route_buy_candidates(db, limit=5, trade_date=DAY,
            account_name="promotion", now=AT)
    assert len(rows) == expected, notes
    if rows:
        assert rows[0]["prediction_run_key"] == "intraday"


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation,allowed", [({}, True), ({"prediction_rank_contract_complete": False}, False),
    ({"prediction_rank_eligible": False}, False), ({"sector_catalyst_spread": False}, False),
    ({"watch_only": True}, False), ({"sector_flow": -1}, False),
    ({"sector_observed_at": (AT+timedelta(seconds=1)).isoformat()}, False), ({"sector_observed_at": None}, False)])
async def test_c_frozen_route_pool_still_needs_live_sector_and_complete_contract(paper_client, monkeypatch, mutation, allowed):
    _, maker = paper_client
    monkeypatch.setattr(paper.trade_calendar, "previous_trade_day", AsyncMock(return_value=DAY-timedelta(days=1)))
    sector = {"sector_trade_date": DAY.isoformat(), "sector_strength": 75, "sector_change_pct": 2,
              "sector_fund_flow": mutation.get("sector_flow", 10), "sector_limit_up_count": 7,
              "sector_observed_at": mutation.get("sector_observed_at", (AT-timedelta(seconds=30)).isoformat())}
    monkeypatch.setattr(paper, "_promotion_mainline_live_sector_context", AsyncMock(return_value=sector))
    factors = {"prediction_rank_contract_version": "promotion_rank_contract_v1",
        "prediction_rank_contract_complete": True, "prediction_rank_eligible": True,
        "broad_rotation_member_setup": True, "sector_catalyst_spread": True,
        "strict_confirmation_count": 3, "sector_strength_score": 70, **mutation}
    async with maker() as db:
        run = _governed_promotion_run(run_key="c-pool", reference_trade_date=DAY,
            snapshot_context="promotion_1000", as_of_at=AT.replace(minute=0))
        db.add(run)
        await db.flush()
        row = _governed_promotion_snapshot(run_id=run.id, record_key="c-unranked", code="600002",
            prediction_trade_date=DAY, probability=.09, target_board=1, route="mainline_spread_start",
            actionable=False, watch_only=mutation.get("watch_only", False))
        row.rank_scope = "pool_unranked"
        row.features_json = json.dumps(factors)
        db.add_all([row, StockSpot(code="600002", name="test", price=10.1, prev_close=10,
                                   change_pct=1, limit_up=11, volume_ratio=1.5)])
        await db.commit()
        candidates, notes = await paper._promotion_route_buy_candidates(db, limit=5, trade_date=DAY,
            account_name="mainline", now=AT)
        assert bool(candidates) is allowed, notes
        assert row.actionable is False and row.rank_scope == "pool_unranked"
        if candidates:
            assert candidates[0]["candidate_eligibility_basis"] == "frozen_route_eligible_live_confirmed"


def test_a_wide_amplitude_has_consistent_vwap_band_without_admitting_weak_or_chasing_quotes(monkeypatch):
    quote = SimpleNamespace(price=10, low=9.85, high=10.6, avg_price=9.98, change_pct=1, limit_up=11)
    monkeypatch.setattr(settings, "PAPER_AUTO_ENTRY_ANCHOR", "legacy_high")
    assert paper._a_entry_price_band(quote)["status"] == "empty"
    assert paper._stable_intraday_entry_quote(quote)[0] is False
    monkeypatch.setattr(settings, "PAPER_AUTO_ENTRY_ANCHOR", "vwap_reclaim")
    assert paper._a_entry_price_band(quote)["status"] == "nonempty"
    assert paper._stable_intraday_entry_quote(quote)[0] is True
    assert paper._stable_intraday_entry_quote(quote, account_name="promotion")[0] is False
    candidate = {"_source": "daily_participation", "execution_confirmation": True}
    assert paper._candidate_execution_value_reject_reason(candidate, price=10, spot=quote) == ""
    quote.price = 9.97
    assert paper._stable_intraday_entry_quote(quote)[0] is False
    quote.price = 10.2
    assert paper._stable_intraday_entry_quote(quote)[0] is False
    quote.avg_price = None
    assert paper._stable_intraday_entry_quote(quote)[0] is False


def test_vwap_anchor_does_not_reintroduce_the_old_day_low_anchor(monkeypatch):
    quote = SimpleNamespace(price=10, low=9.5, high=10.4, avg_price=9.98, change_pct=1, limit_up=11)
    candidate = {"_source": "daily_participation", "execution_confirmation": True}
    assert (quote.price/quote.low-1)*100 > 3
    assert paper._a_entry_price_band(quote)["status"] == "nonempty"
    assert paper._candidate_execution_value_reject_reason(candidate, price=10, spot=quote) == ""
    monkeypatch.setattr(settings, "PAPER_AUTO_ENTRY_ANCHOR", "legacy_high")
    assert "从日内低点反弹" in paper._candidate_execution_value_reject_reason(candidate, price=10, spot=quote)


@pytest.mark.parametrize("elapsed,layers,price,allowed", [(1860, 1, 10.05, True), (1859, 1, 10.05, False),
    (1860, 2, 10.05, False), (1860, 1, 9.99, False), (1860, 1, 10.2, False)])
def test_same_day_scale_requires_new_confirmation_after_cooldown(elapsed, layers, price, allowed):
    candidate = {"_source": "daily_participation", "execution_confirmation": True,
        "confirmation_sample_at": AT.isoformat(), "confirmation_sample_count": 2,
        "confirmation_persistence_sec": 60, "stop_loss_price": 9.6}
    pos = PaperPosition(buy_price=10, buy_amount=500, stop_loss_price=9.6)
    reason = paper._scale_in_reject_reason(candidate, position=pos, price=price, score=90,
        total_assets=50000, bought_code_today=True, now=AT, last_buy_at=AT-timedelta(seconds=elapsed), daily_layers=layers)
    assert (reason == "") is allowed


def test_scale_total_risk_and_total_position_budget_include_existing_lots():
    pos = PaperPosition(buy_price=10, buy_amount=900, stop_loss_price=9)
    # 5万*2%=1000风险额度，已有900股，每股风险1元，最多再买100股。
    assert paper._scale_in_risk_amount(700, position=pos, price=10, stop_loss=9, total_assets=50000) == 100
    assert paper._scale_in_risk_amount(700, position=pos, price=10, stop_loss=10, total_assets=50000) == 0
    account = SimpleNamespace(total_assets=50000, current_capital=40000, initial_capital=50000, max_drawdown=0)
    amount = paper._staged_entry_buy_amount(account, 10, 95, pos)
    assert (900+amount)*10 <= 50000*settings.PAPER_AUTO_STAGED_ENTRY_MAX_POSITION_PCT


@pytest.mark.asyncio
async def test_legacy_shared_signal_without_receipts_cannot_prove_one_order(paper_client):
    _, maker = paper_client
    async with maker() as db:
        trades = [PaperTradeLog(account_id=7, code="600001", trade_type="buy", price=10, amount=100,
            trade_time=AT-timedelta(minutes=40-i), signal_id="same-order") for i in range(2)]
        db.add_all(trades)
        await db.flush()
        for trade in trades[:1]:
            db.add(PaperAutoTradeLog(account_id=7, trade_date=DAY, run_id=f"r{trade.id}", source="daily_participation",
                code="600001", action="buy", decision="executed", executed_trade_id=trade.id,
                created_at=AT, candidate_json=json.dumps({"scale_in": True})))
        db.add(PaperAutoTradeLog(account_id=7, trade_date=DAY, run_id="new", source="daily_participation",
            code="600002", action="buy", decision="executed", created_at=AT, candidate_json="{}"))
        await db.commit()
        rows = await paper._today_auto_new_buy_logs(db, DAY, account_id=7, as_of=AT)
        # Primary quota requires original-order authority; a bare legacy flag
        # cannot free its slot. Connected legacy fallback is unchanged.
        assert {row.code for row in rows} == {"600001", "600002"}
        # No TradeFill/order evidence: connected exact audit excludes only its own row.
        assert await _today_buy_count(db, 7, DAY) == 1
        layers, last_at = await paper._same_day_buy_layers(db, account_id=7, code="600001", now=AT)
        assert layers == 2 and last_at == AT-timedelta(minutes=39)


@pytest.mark.asyncio
async def test_order_signal_without_receipt_cannot_exempt_a_legacy_buy(paper_client):
    _, maker = paper_client
    async with maker() as db:
        account = await paper._get_or_create_account(db, "challenger_b")
        db.add_all([
            PaperTradeLog(account_id=account.id, code="600001", trade_type="buy", price=10,
                amount=100, trade_time=AT, signal_id="partial-add"),
            TradeOrder(order_id="add-order", broker="paper", account_id="challenger_b", code="600001",
                side="buy", order_type="limit", quantity=300, filled_quantity=100, price=10,
                status="partial", trade_date=DAY, signal_id="partial-add",
                risk_json=json.dumps({"paper_deferred_order": {"candidate": {"scale_in": True}}})),
        ])
        await db.commit()
        assert await _today_buy_count(db, account.id, DAY) == 1  # matching signal text is not a receipt


@pytest.mark.asyncio
async def test_a_scale_passes_full_new_name_and_sector_quotas(paper_client, monkeypatch):
    _, maker = paper_client
    monkeypatch.setattr(settings, "PAPER_AUTO_MAX_DAILY_NEW_BUYS", 1)
    monkeypatch.setattr(settings, "PAPER_AUTO_MAX_POSITIONS", 1)
    monkeypatch.setattr(paper, "_paper_now", lambda: AT)
    candidate = {"code": "600001", "name": "test", "_source": "daily_participation",
        "total_score": 95, "execution_confirmation": True, "daily_participation": True,
        "sector_name": "same-sector", "stop_loss_price": 9.5,
        "confirmation_sample_at": AT.isoformat(), "confirmation_sample_count": 2,
        "confirmation_persistence_sec": 60}
    monkeypatch.setattr(paper, "_paper_auto_buy_candidates", AsyncMock(return_value=([candidate], [])))
    monkeypatch.setattr(paper, "_confirm_candidate_main_fund", AsyncMock(return_value=""))
    monkeypatch.setattr(paper, "_continuation_risk_reject_reason", AsyncMock(return_value=""))
    monkeypatch.setattr(paper, "_execution_quote_status", lambda *args: (True, ""))
    monkeypatch.setattr(paper, "_risk_check_for_buy", AsyncMock(return_value={"final_level": "pass", "block_reasons": [], "warnings": []}))
    async with maker() as db:
        account = await paper._get_or_create_account(db, "default")
        version = paper._strategy_version("default")
        bought_at = AT-timedelta(minutes=35)
        db.add_all([
            PaperPosition(account_id=account.id, code="600001", name="test", buy_price=10,
                buy_amount=100, current_price=10, buy_time=bought_at, strategy_version=version, stop_loss_price=9.5, is_closed=False),
            PaperTradeLog(account_id=account.id, code="600001", trade_type="buy", price=10, amount=100,
                trade_time=bought_at, signal_id="first", strategy_version=version),
            PaperAutoTradeLog(account_id=account.id, trade_date=DAY, run_id="first", source="daily_participation",
                code="600001", action="buy", decision="executed", candidate_json=json.dumps({"sector_name": "same-sector"})),
            StockSpot(code="600001", name="test", price=10, avg_price=10, high=10.1, low=9.8,
                change_pct=.5, volume_ratio=1.2, ask1_price=10.01, bid1_price=9.99, limit_up=11, limit_down=9, updated_at=AT),
        ])
        await db.commit()
        result = await paper.run_paper_auto_trade(db, now=AT, execute=False, execution_mode="manual",
                                                include_position_risk=False, account_name="default")
        buys = [row for row in result["logs"] if row["action"] == "buy" and row["decision"] == "dry_run"]
        assert len(buys) == 1, result["logs"]
        assert candidate["scale_in"] is True
