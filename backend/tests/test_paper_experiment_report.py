from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy import func, select

from app.api.v1 import paper
from app.config.settings import settings
from app.db.session import Base
from app.models.paper import PaperAccount, PaperAutoTradeLog
from app.models.regime import MarketRegimeSnapshot
from app.models.risk import DataSourceHealth
from app.models.stock import StockDaily
from app.models.governance import TradeCalendarModel
from app.paper.experiment import freeze_entry_evidence
from app.paper.experiment_report import complete_cycles, build_experiment_report
from app.paper.experiment_regime import classify_index_closes, freeze_benchmark_regime


@pytest_asyncio.fixture
async def db(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'report.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


def trade(id, side, qty, price, *, version="v1", forced=False):
    return SimpleNamespace(id=id, code="600001", trade_type=side, amount=qty, price=price,
        trade_time=datetime(2026, 9, 8 + id, 10), strategy_version=version,
        forced_probe=forced, excluded_from_performance=False, commission=5, tax=0)


def evidence(version="v1"):
    return {"active": True, "strategy_version": version,
        "entry_regime": {"label": "broad_trend"},
        "entry_bull_bear": {"label": "bull"},
        "entry_sentiment": {"phase": "climax", "quality_status": "ok"}}


def test_round_trip_counts_once_after_final_partial_sell_and_deducts_all_fees():
    rows = [trade(1, "buy", 200, 10), trade(2, "sell", 100, 11)]
    closed, excluded, pending = complete_cycles(rows, {"1": evidence()}, version="v1", start_date="2026-09-08")
    assert not closed and not excluded and pending == 1
    rows.append(trade(3, "sell", 100, 9.2))
    closed, excluded, pending = complete_cycles(rows, {"1": evidence()}, version="v1", start_date="2026-09-08")
    assert len(closed) == 1 and not excluded and not pending
    assert closed[0]["net_pnl"] == 5  # 1100+920-2000-3*5
    assert closed[0]["entry_regime"] == "broad_trend"
    assert closed[0]["first_buy_trade_id"] == 1
    assert closed[0]["exit_trade_id"] == 3
    assert closed[0]["final_exit_quantity"] == 100
    assert closed[0]["entry_basis_before_final_exit"] == 10
    assert closed[0]["trade_ids"] == [1, 2, 3]
    assert closed[0]["actual_fees"] == 15
    assert closed[0]["final_exit_net_cash"] == pytest.approx(915)


@pytest.mark.parametrize("bad_kind", ["version", "forced", "missing_evidence"])
def test_mixed_or_forced_or_unaudited_cycle_never_becomes_current_protocol_win(bad_kind):
    rows = [trade(1, "buy", 100, 10), trade(2, "buy", 100, 9,
            version="old" if bad_kind == "version" else "v1", forced=bad_kind == "forced"),
            trade(3, "sell", 200, 11)]
    snapshots = {"1": evidence(), "2": evidence()}
    if bad_kind == "missing_evidence":
        snapshots.pop("2")
    audit = []
    closed, excluded, pending = complete_cycles(rows, snapshots, version="v1", start_date="2026-09-08", audit_cycles=audit)
    assert not closed and excluded == 1 and not pending
    assert len(audit) == 1 and audit[0]["complete"] is True
    assert audit[0]["exit_trade_id"] == 3
    assert audit[0]["exclusion_reasons"]


@pytest.mark.parametrize("quantity", [True, False, None, "bad", float("nan"), float("inf"), 1.5, -100, 0])
def test_dirty_quantity_quarantines_only_affected_code(quantity):
    rows = [trade(1, "buy", 200, 10), trade(2, "sell", quantity, 11),
            trade(3, "sell", 200, 11), trade(4, "buy", 100, 10), trade(5, "sell", 100, 11)]
    rows[-2].code = rows[-1].code = "600002"
    audit = []
    cycles, excluded, pending = complete_cycles(rows, {"1": evidence(), "4": evidence()},
        version="v1", start_date="2026-09-08", audit_cycles=audit)
    assert len(cycles) == 1 and cycles[0]["code"] == "600002"
    assert excluded == 2 and pending == 0
    assert all(not c["complete"] for c in audit if c["code"] == "600001")


@pytest.mark.parametrize("raw", [[], "bad", None, True,
    {"active": True, "strategy_version": "v1", "entry_sentiment": [], "entry_regime": True, "entry_bull_bear": "bad"}])
def test_non_dict_entry_evidence_never_aborts_cycle_report(raw):
    audit = []
    complete_cycles([trade(1, "buy", 100, 10), trade(2, "sell", 100, 11)],
        {"1": raw}, version="v1", start_date="2026-09-08", audit_cycles=audit)
    assert len(audit) == 1
    assert audit[0]["entry_regime"] == audit[0]["entry_bull_bear"] == audit[0]["entry_sentiment"] == "unknown"


def test_reentry_same_code_preserves_two_liquidation_ids_and_audit_is_owned():
    rows = [trade(1, "buy", 100, 10), trade(2, "sell", 100, 11),
            trade(3, "buy", 100, 9), trade(4, "sell", 100, 10)]
    audit = []
    cycles, excluded, pending = complete_cycles(rows, {"1": evidence(), "3": evidence()},
        version="v1", start_date="2026-09-08", audit_cycles=audit)
    assert not excluded and not pending
    assert [(c["first_buy_trade_id"], c["exit_trade_id"]) for c in cycles] == [(1, 2), (3, 4)]
    audit[0]["trade_ids"].append(999)
    assert cycles[0]["trade_ids"] == [1, 2]


@pytest.mark.asyncio
async def test_empty_report_has_twelve_null_win_rates_and_creates_no_accounts(db, monkeypatch):
    monkeypatch.setattr(settings, "PAPER_CONTINUOUS_EXPERIMENT_ENABLED", True)
    monkeypatch.setattr(settings, "PAPER_EXPERIMENT_START_DATE", "2026-09-08")
    monkeypatch.setattr(settings, "PAPER_EXPERIMENT_ACTIVATION_AT", "2026-09-08T00:00:00")
    result = await build_experiment_report(db, now=datetime(2026, 9, 8, 10))
    assert len(result["accounts"]) == 12
    assert all(item["account_id"] is None for item in result["accounts"])
    assert all(item["win_rate"] is None and item["closed_round_trips"] == 0 for item in result["accounts"])
    assert all(item["active"] and item["auto_buy_enabled"] for item in result["accounts"])
    assert await db.scalar(select(func.count(PaperAccount.id))) == 0


@pytest.mark.asyncio
async def test_report_account_id_is_latest_active_instance_not_latest_closed(db):
    rows = [PaperAccount(account_name="default", initial_capital=10000, status=status)
            for status in ("active", "active", "closed")]
    db.add_all(rows)
    await db.flush()
    result = await build_experiment_report(db, account_name="default", now=datetime(2026, 9, 8, 10))
    assert result["accounts"][0]["account_id"] == rows[1].id
    assert result["accounts"][0]["account_name"] == "default"
    assert await db.scalar(select(func.count(PaperAccount.id))) == 3


@pytest.mark.asyncio
async def test_freeze_ignores_future_regime_and_preserves_unknown_benchmark(db, monkeypatch):
    monkeypatch.setattr(paper.trade_calendar, "previous_trade_day", AsyncMock(side_effect=AssertionError("入场审计不能联网加载日历")))
    db.add(TradeCalendarModel(trade_date=date(2026, 9, 7), is_trade_day=True))
    for key, asof, label in [
        ("known", datetime(2026, 9, 7, 16), "risk_off"),
        ("future", datetime(2026, 9, 8, 15), "broad_trend"),
    ]:
        db.add(MarketRegimeSnapshot(snapshot_key=key, trade_date=asof.date(), as_of_at=asof,
            created_at=asof, regime_version="test", data_version="test", primary_regime=label,
            confidence=.8, quality_status="good"))
    await db.flush()
    frozen = await freeze_entry_evidence(db, "challenger_e", at=datetime(2026, 9, 8, 10), sentiment={})
    assert frozen["entry_regime"]["snapshot_key"] == "known"
    assert frozen["entry_regime"]["label"] == "risk_off"
    assert frozen["entry_regime"]["scope"] == "prior_session"
    assert frozen["entry_bull_bear"]["label"] == "unknown"


@pytest.mark.asyncio
async def test_activity_uses_scan_heartbeats_not_signal_or_exit_run_counts(db):
    at = datetime(2026, 9, 8, 10)
    account = PaperAccount(account_name="challenger_a", initial_capital=50000, status="active")
    db.add(account)
    await db.flush()
    for run_id, action in [("scan-r1", "scan"), ("scan-r2", "scan"),
                           ("signal-one", "wait_buy"), ("exit-one", "hold"),
                           ("bp-one", "buy_signal"), ("push-one", "signal_push")]:
        db.add(PaperAutoTradeLog(account_id=account.id, run_id=run_id,
            trade_date=at.date(), created_at=at, action=action, decision="wait",
            strategy_version=paper._strategy_version("challenger_a"), reason="测试"))
    await db.flush()
    report = await build_experiment_report(db, account_name="challenger_a", now=at)
    activity = report["accounts"][0]["latest_activity"]
    assert activity["scan_count"] == 2
    assert activity["scan_count_basis"] == "scan_heartbeats"
    assert activity["decision_count"] == 2  # 两条扫描心跳不是策略决策


@pytest.mark.asyncio
async def test_entry_calendar_gap_is_unknown_not_stale_day_or_network_fallback(db, monkeypatch):
    from app.paper.experiment_regime import previous_known_trade_day
    monkeypatch.setattr(paper.trade_calendar, "previous_trade_day", AsyncMock(side_effect=AssertionError("network fallback forbidden")))
    db.add(TradeCalendarModel(trade_date=date(2026, 9, 4), is_trade_day=True))
    await db.flush()
    assert await previous_known_trade_day(db, at=datetime(2026, 9, 7, 10)) == date(2026, 9, 4)
    assert await previous_known_trade_day(db, at=datetime(2026, 9, 8, 10)) is None
    frozen = await freeze_entry_evidence(db, "challenger_e", at=datetime(2026, 9, 8, 10), sentiment={})
    assert frozen["entry_bull_bear"]["label"] == "unknown"
    assert frozen["entry_bull_bear"]["source_trade_date"] is None
    assert "日历" in frozen["entry_bull_bear"]["reason"]


def test_index_health_cannot_certify_previous_day_fallback_or_invalid_current_close():
    from app.data.scheduler import _current_index_snapshot_records
    at = date(2026, 9, 8)
    rows = [
        {"code": "000001", "trade_date": at, "close": 3000, "change_pct": 1},
        {"code": "399001", "trade_date": at - timedelta(days=1), "close": 3000, "change_pct": 1},
        {"code": "399006", "trade_date": at, "close": None, "change_pct": 1},
        {"code": "000001", "trade_date": at, "close": 3001, "change_pct": 1},
    ]
    selected = _current_index_snapshot_records(rows, at)
    assert len(selected) == 1 and selected[0]["close"] == 3001
    rows[1]["trade_date"] = at
    rows[2]["close"] = float("inf")
    assert len(_current_index_snapshot_records(rows, at)) == 2
    rows[2]["close"] = 1500
    assert len(_current_index_snapshot_records(rows, at)) == 3


def test_frozen_bull_bear_definition_and_missing_bar():
    assert classify_index_closes([3000+i for i in range(65)])["label"] == "bull"
    assert classify_index_closes([4000-i for i in range(65)])["label"] == "bear"
    assert classify_index_closes([3000]*65)["label"] == "sideways"
    assert classify_index_closes([3000]*64)["label"] == "unknown"


@pytest.mark.asyncio
async def test_benchmark_uses_previous_complete_session_not_todays_future_bar(db):
    previous = date(2026, 9, 7)
    days = []
    cursor = previous
    from app.core.trade_calendar import is_official_closed_day
    while len(days) < 65:
        if cursor.weekday() < 5 and not is_official_closed_day(cursor):
            days.append(cursor)
        cursor -= timedelta(days=1)
    days.reverse()
    for i, day in enumerate(days):
        db.add(TradeCalendarModel(trade_date=day, is_trade_day=True))
        for code in ("000001", "399001"):
            db.add(StockDaily(code=code, trade_date=day, close=3000+i))
    prior_close_health = DataSourceHealth(
        source="index", api_name="daily_snapshot", status="up",
        last_success=datetime(2026, 9, 7, 15, 10), completeness=1,
    )
    db.add(prior_close_health)
    await db.flush()
    prior_close_health_id = prior_close_health.id
    # 健康记录是追加式；次日盘中新记录不能替代或使昨收证据失效。
    db.add(DataSourceHealth(
        source="index", api_name="daily_snapshot", status="up",
        last_success=datetime(2026, 9, 8, 9, 35), completeness=1,
    ))
    db.add(StockDaily(code="000001", trade_date=date(2026, 9, 8), close=1))  # 今日未完bar不得影响入场标签
    await db.flush()
    result = await freeze_benchmark_regime(db, at=datetime(2026, 9, 8, 10), previous_trade_date=previous)
    # 旧daily_snapshot只证明一根日线采集，不能替代65日首次可知证据。
    assert result["label"] == "unknown"
    from app.data.index_history_window import build_history_evidence
    from app.data.index_history import HISTORY_API, INDEX_SYMBOLS
    import pandas as pd
    import json
    observed = datetime(2026, 9, 7, 17)
    frames = {}
    for code, symbol in INDEX_SYMBOLS.items():
        frame = pd.DataFrame({"date": days, "close": [3000+i for i in range(65)]})
        frame.attrs.update(source_contract="tencent_index_raw_day_v1", index_symbol=symbol,
                           source_observed_at=observed.isoformat())
        frames[code] = frame
    evidence = build_history_evidence(frames, expected_days=days, through_date=previous, observed_at=observed)
    history_health = DataSourceHealth(source="index", api_name=HISTORY_API, status="up",
        completeness=1, last_success=observed, updated_at=observed, error_msg=json.dumps(evidence))
    db.add(history_health)
    await db.flush()
    result = await freeze_benchmark_regime(db, at=datetime(2026, 9, 8, 10), previous_trade_date=previous)
    assert result["label"] == "bull" and result["quality_status"] == "ok"
    assert result["source_health_id"] == history_health.id != prior_close_health_id
    assert all(item["close"] == 3064 for item in result["benchmarks"])
