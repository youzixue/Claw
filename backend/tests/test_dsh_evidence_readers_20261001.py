"""All mutations are synthetic temp fixtures; readers use independent mode=ro."""
import hashlib
import json
from datetime import date, datetime
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.governance import TradeCalendarModel
from app.models.paper import PaperAccount, PaperAutoTradeLog, PaperDailyOutcome, PaperPosition, PaperTradeLog, PaperSaleAccounting
from app.models.stock import StockKline, QuoteRound, LimitUpPool, BrokenLimitPool, StockKlineObservation
from app.models.review import DailyReviewSnapshot
from app.models.trading import TradeOrder, TradeFill
from app.review.evidence_store import evidence_session, bounded, cutoff, trade_day
from app.review.evidence_readers import calendar_context, readiness, execution_evidence, decision_trace, market_universe
from app.review.price_evidence import price_evidence, _aggregate_five
from app.review.research_artifacts import read_artifact, artifact_catalog
from app.review.evidence_dispatch import read_local

DAY = date(2026,9,30)
AT = datetime(2026,9,30,21,45)
TABLES = [m.__table__ for m in (
    TradeCalendarModel, PaperAccount, PaperAutoTradeLog, PaperDailyOutcome,
    PaperPosition, PaperTradeLog, PaperSaleAccounting, StockKline, QuoteRound,
    LimitUpPool, BrokenLimitPool, StockKlineObservation, DailyReviewSnapshot,
    TradeOrder, TradeFill)]


@pytest.fixture(scope="session", autouse=True)
def isolated_test_database_guard():
    # No global startup initializer: this file builds only its explicit temp tables.
    yield


@pytest_asyncio.fixture
async def database(tmp_path):
    path = tmp_path/"isolated.db"
    url = f"sqlite+aiosqlite:///{path}"
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        from app.db.session import Base
        await conn.run_sync(lambda sync: Base.metadata.create_all(sync,tables=TABLES))
    async with async_sessionmaker(engine,expire_on_commit=False)() as db:
        db.add_all([
            TradeCalendarModel(trade_date=date(2026,9,29),is_trade_day=True),
            TradeCalendarModel(trade_date=DAY,is_trade_day=True),
            TradeCalendarModel(trade_date=date(2026,10,1),is_trade_day=False),
            PaperAccount(id=1,account_name="default",initial_capital=10000,
                         current_capital=10580,total_assets=10580,status="active"),
            PaperTradeLog(id=1,account_id=1,code="600001",trade_type="buy",price=10,
                amount=100,trade_time=datetime(2026,9,29,10),commission=10,tax=0,
                strategy_version="old",forced_probe=False,excluded_from_performance=False),
            PaperTradeLog(id=2,account_id=1,code="600001",trade_type="sell",price=16,
                amount=100,trade_time=datetime(2026,9,30,10),commission=10,tax=0,
                realized_pnl=590,strategy_version="old",forced_probe=False,excluded_from_performance=False),
            StockKline(code="600001",trade_date=DAY,open=10,close=16,high=16,low=9,
                prev_close=10,change_pct=60,source="ths"),
            StockKline(code="600002",trade_date=DAY,open=10,close=9,high=11,low=9,
                prev_close=10,change_pct=-10,source="ths"),
            StockKline(code="600001",trade_date=date(2026,9,29),open=10,close=10,
                high=11,low=9,prev_close=10,change_pct=0,source="tencent_close"),
            LimitUpPool(code="600003",trade_date=DAY,name="missing K",quarantined=False),
            PaperAutoTradeLog(id=1,account_id=1,run_id="r1",trade_date=DAY,
                created_at=datetime(2026,9,30,10),code="600001",source="candidate",
                action="candidate_audit",decision="blocked",risk_json='{"gate":"blocked"}',
                candidate_json='{"candidate_trace":{"reused_log_ids":[7]}}',strategy_version="old"),
        ])
        await db.commit()
    await engine.dispose()
    yield url, path


@pytest.mark.asyncio
async def test_modes_and_write_block_with_content_unchanged(database):
    url,path=database
    before=hashlib.sha256(path.read_bytes()).hexdigest()
    async with evidence_session(url) as db:
        assert db.autoflush is False
        assert await db.scalar(select(PaperAccount.account_name))=="default"
        for sql in ("CREATE TABLE forbidden(a)", "UPDATE paper_account SET current_capital=0",
                    "DELETE FROM paper_trade_log", "ATTACH DATABASE ':memory:' AS tmp", "PRAGMA query_only=OFF"):
            with pytest.raises(RuntimeError,match="readonly_statement_denied"):
                await db.execute(text(sql))
    assert hashlib.sha256(path.read_bytes()).hexdigest()==before


@pytest.mark.asyncio
async def test_holiday_and_calendar_gap_never_assumes_open(database):
    url,_=database
    async with evidence_session(url) as db:
        known=await calendar_context(db,date(2026,10,1))
        assert known["status"]=="non_trading_day"
        unknown=await calendar_context(db,date(2026,10,5))
        assert unknown["status"]=="calendar_unknown"
        assert unknown["expected_previous_trade_date"] is None
        assert (await readiness(db,day=date(2026,10,1),at=datetime(2026,10,1,22),phase="postmarket"))["status"]=="skipped_non_trading_day"


@pytest.mark.asyncio
async def test_missing_readiness_partial_and_late_premarket_blocked(database):
    url,_=database
    async with evidence_session(url) as db:
        value=await readiness(db,day=DAY,at=AT,phase="postmarket")
        assert value["status"]=="partial"
        assert len(value["components"]["daily_outcomes"]["missing_accounts"])==12
        value=await readiness(db,day=DAY,at=AT,phase="premarket")
        assert value["status"]=="blocked_after_premarket_deadline"


@pytest.mark.asyncio
@pytest.mark.parametrize("hour,minute,second,expected", [
    (15, 29, 59, "blocked_before_close"),
    (15, 30, 0, "partial"),
    (15, 45, 0, "partial"),
])
async def test_1530_research_window_does_not_finalize_or_read_later_material(
        database, monkeypatch, hour, minute, second, expected):
    from app.review import research_artifacts
    url, path = database
    # Publication is not requested; this synthetic catalog has no stored files.
    monkeypatch.setattr(research_artifacts, "artifact_catalog",
                        lambda *args, **kwargs: {"items": []})
    engine = create_async_engine(url)
    async with async_sessionmaker(engine)() as db:
        db.add_all([
            DailyReviewSnapshot(review_key="late-close", review_date=DAY,
                analysis_trade_date=DAY, phase="postmarket",
                as_of_at=datetime(2026, 9, 30, 15, 20),
                created_at=datetime(2026, 9, 30, 20, 35),
                schema_version="test", data_version="test", quality_status="good"),
            PaperDailyOutcome(account_id=1, trade_date=DAY, strategy_version="test",
                is_terminal=True, finalized_at=datetime(2026, 9, 30, 15, 50)),
        ])
        await db.commit()
    await engine.dispose()
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    async with evidence_session(url) as db:
        value = await readiness(db, day=DAY,
            at=datetime(2026, 9, 30, hour, minute, second), phase="postmarket")
    assert value["status"] == expected
    assert value["components"]["daily_kline"]["status"] == "available"
    assert value["components"]["snapshot"]["status"] == "missing"
    assert value["components"]["daily_outcomes"]["available_accounts"] == []
    assert len(value["components"]["daily_outcomes"]["missing_accounts"]) == 12
    assert value["components"]["research_artifacts"]["status"] == "missing"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


@pytest.mark.asyncio
async def test_actual_all_version_accounting_and_missing_accounts(database):
    url,_=database
    async with evidence_session(url) as db:
        value=await execution_evidence(db,day=DAY,at=AT)
        assert len(value["accounts"])==12
        a=value["accounts"][0]
        assert a["accounting"]["today_realized_net_pnl"]==580
        assert a["accounting"]["closed_cycle_performance"]["sample_count"]==1
        assert value["accounts"][1]["status"]=="account_missing_or_ambiguous"
        cycles=await execution_evidence(db,day=DAY,at=AT,account_name="default",section="cycles")
        assert cycles["items"][0]["strategy_versions"]==["old"]
        assert cycles["items"][0]["realized_net_pnl"]==580
        # Current projection is never forged into historical cash/holdings.
        old=await execution_evidence(db,day=date(2026,9,29),at=datetime(2026,9,29,22),account_name="default")
        assert old["accounts"][0]["accounting"] is None


@pytest.mark.asyncio
async def test_trades_keyset_paging_and_scope(database):
    url,_=database
    async with evidence_session(url) as db:
        result=await execution_evidence(db,day=DAY,at=AT,section="trades",limit=1)
        assert [r["id"] for r in result["items"]]==[2]
        assert result["next_cursor"] is None
        assert not (await execution_evidence(db,day=DAY,at=AT,section="trades",account_name="challenger_a"))["items"]


@pytest.mark.asyncio
async def test_closed_same_name_is_history_not_active_ambiguity(database):
    url, path = database
    engine = create_async_engine(url)
    async with async_sessionmaker(engine)() as db:
        db.add(PaperAccount(id=2, account_name="default", status="closed",
                           initial_capital=1000000, current_capital=900000, total_assets=900000))
        db.add(PaperTradeLog(id=3, account_id=2, code="600002", trade_type="buy",
                            price=10, amount=100, trade_time=datetime(2026,9,30,9,45)))
        await db.commit()
    await engine.dispose()
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    async with evidence_session(url) as db:
        entry = (await execution_evidence(db, day=DAY, at=AT,
                                          account_name="default"))["accounts"][0]
        assert entry["status"] == "stored_projection"
        assert entry["account_id"] == 1
        assert entry["active_instance_count"] == 1 and entry["instance_count"] == 2
        assert entry["historical_accounts"] == [{"account_id": 2, "status": "closed"}]
        assert entry["all_version_trade_count"] == 2
        assert entry["stored_account"]["initial_capital"] == 10000
        cycles = await execution_evidence(db, day=DAY, at=AT,
                                          account_name="default", section="cycles")
        assert cycles["account_id"] == 1 and len(cycles["items"]) == 1
        trades = await execution_evidence(db, day=DAY, at=AT,
                                          account_name="default", section="trades")
        assert {row["account_id"] for row in trades["items"]} == {1, 2}
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("statuses", [("closed",), ("active", "active")])
async def test_no_unique_active_identity_fails_closed_but_keeps_history(database, statuses):
    url, _ = database
    engine = create_async_engine(url)
    async with async_sessionmaker(engine)() as db:
        account = await db.get(PaperAccount, 1)
        account.status = statuses[0]
        if len(statuses) == 2:
            db.add(PaperAccount(id=2, account_name="default", status=statuses[1],
                               initial_capital=50000, current_capital=50000, total_assets=50000))
        await db.commit()
    await engine.dispose()
    async with evidence_session(url) as db:
        summary = await execution_evidence(db, day=DAY, at=AT, account_name="default")
        assert summary["accounts"][0]["status"] == "account_missing_or_ambiguous"
        assert "stored_account" not in summary["accounts"][0]
        cycles = await execution_evidence(db, day=DAY, at=AT,
                                          account_name="default", section="cycles")
        assert cycles["status"] == "account_missing_or_ambiguous"
        positions = await execution_evidence(db, day=DAY, at=AT,
                                             account_name="default", section="positions")
        assert positions["unresolved_accounts"] == ["default"]
        assert positions["items"] == []
        assert len((await execution_evidence(db, day=DAY, at=AT,
                    account_name="default", section="trades"))["items"]) == 1


@pytest.mark.asyncio
async def test_trace_keeps_risk_candidate_and_missing_unknown(database):
    url,_=database
    async with evidence_session(url) as db:
        result=await decision_trace(db,day=DAY,at=AT,code="600001")
        assert result["items"][0]["risk_json"]=={"gate":"blocked"}
        assert result["items"][0]["candidate_json"]["candidate_trace"]["reused_log_ids"]==[7]
        empty=await decision_trace(db,day=DAY,at=AT,code="600999")
        assert empty["items"]==[]
        assert empty["missing_trace"].startswith("unknown")


@pytest.mark.asyncio
async def test_universe_keeps_losers_missing_bars_and_basis(database):
    url,_=database
    async with evidence_session(url) as db:
        page=await market_universe(db,day=DAY,at=AT,limit=2)
        assert page["total_stored_universe"]==3
        assert page["counts"]["down"]==1
        assert page["items"][0]["mixed_or_unknown_basis"] is True
        next_page=await market_universe(db,day=DAY,at=AT,cursor=page["next_cursor"],limit=2)
        assert next_page["items"][0]["code"]=="600003"
        assert next_page["items"][0]["day_kline"] is None
        assert next_page["full_market_strategy_shape_control"].startswith("unavailable")


@pytest.mark.asyncio
async def test_no_daily_close_before_close_or_download_missing_minutes(database):
    url,_=database
    async with evidence_session(url) as db:
        assert (await price_evidence(db,day=DAY,at=datetime(2026,9,30,10),code="600001"))["status"]=="unavailable"
        result=await price_evidence(db,day=DAY,at=AT,code="600001",period="sampled_1m",archive_root=Path(url.split("///")[1]).parent/"missing")
        assert result["status"]=="partial"
        assert len(result["missing_regular_minutes"])==240
        assert result["full_exchange_bars"] is False


def test_sampled_five_never_sums_cumulative_volume():
    rows=[{"minute":f"2026-09-30T09:3{i}:00","open":10,"high":11,"low":9,"close":10+i,
           "volume":100+i*10,"amount":1000+i*100} for i in range(3)]
    result=_aggregate_five(rows)[0]
    assert result["sampled_minutes"]==3
    assert result["complete_five_sampled_minutes"] is False
    assert result["minute_volume"] is None
    assert result["cumulative_session_volume_at_end"]==120


def test_published_artifact_identity_clock_and_pagination(tmp_path):
    data={"as_of":"2026-09-30T20:45:00.000001","schema":"paper_daily_research_v1",
          "read_only":True,"sections":{"post_exit":{"status":"built","report":{"cycles":[
            {"account_name":"default","code":"600001"},{"account_name":"challenger_a","code":"600002"}]}}}}
    raw=(json.dumps(data)+"\n").encode()
    name="paper-research-20260930T204500000001-"+hashlib.sha256(raw).hexdigest()[:16]+".json"
    (tmp_path/name).write_bytes(raw)
    value=read_artifact(DAY,as_of=AT,section="post_exit",account_name="default",root=tmp_path)
    assert value["total_unfiltered"]==2 and value["total_matching"]==1
    assert value["items"][0]["code"]=="600001"
    assert not artifact_catalog(DAY,as_of=datetime(2026,9,30,20),root=tmp_path)["items"]
    with pytest.raises(ValueError):
        read_artifact(DAY,as_of=AT,artifact_id="../claw.db",root=tmp_path)
    (tmp_path/name).write_bytes(b"{}")
    with pytest.raises(ValueError,match="hash"):
        read_artifact(DAY,as_of=AT,artifact_id=name,root=tmp_path)


@pytest.mark.asyncio
async def test_missing_table_and_missing_db_do_not_initialize(tmp_path):
    path=tmp_path/"empty.db"
    path.touch()
    value=await read_local("paper_execution_evidence",{"trade_date":"2026-09-30"},database_url=f"sqlite+aiosqlite:///{path}")
    assert value["status"]=="unavailable"
    assert value["no_refresh_or_initialization_attempted"] is True
    assert path.stat().st_size==0
    missing=tmp_path/"does-not-exist.db"
    with pytest.raises(ValueError):
        async with evidence_session(f"sqlite+aiosqlite:///{missing}"):
            pytest.fail("entered missing storage")
    assert not missing.exists()


def test_cutoff_and_cell_budget():
    assert cutoff("2026-09-30T00:00:00Z")==datetime(2026,9,30,8)
    with pytest.raises(ValueError): cutoff("2999-01-01T00:00:00+08:00")
    with pytest.raises(ValueError): trade_day("2026-10-01",as_of=AT)
    assert bounded({"raw":"x"*30000})["raw"]["status"]=="cell_omitted_byte_budget"


@pytest.mark.asyncio
async def test_market_section_dispatch_preserves_ro_session_and_legacy_page(database, monkeypatch):
    from app.review import market_batch
    url, _ = database
    calls = []
    async def batch(db, **kwargs):
        assert db.autoflush is False and db.in_transaction()
        assert await db.scalar(text("SELECT query_only FROM pragma_query_only")) == 1
        calls.append(kwargs)
        return {"status": "partial", "read_only": True, "truncated": True}
    monkeypatch.setattr(market_batch, "read_market_batch", batch)
    args = {"trade_date": DAY.isoformat(), "as_of": AT.isoformat(),
            "section": "features", "cohort": "non_rising", "cursor": "600001",
            "limit": 2, "include_history": False}
    value = await read_local("ashare_market_review_universe", args, database_url=url)
    assert value["truncated"] is True
    assert calls == [{"day": DAY, "at": AT, "section": "features", "cohort": "non_rising",
                      "cursor": "600001", "limit": 2}]
    raw = await read_local("ashare_market_review_universe",
        {"trade_date": DAY.isoformat(), "as_of": AT.isoformat(), "limit": 2}, database_url=url)
    assert raw["total_stored_universe"] == 3
    assert len(calls) == 1
    with pytest.raises(ValueError, match="cohort filters require"):
        await read_local("ashare_market_review_universe",
            {"trade_date": DAY.isoformat(), "as_of": AT.isoformat(), "section": "page",
             "cohort": "non_rising"}, database_url=url)
