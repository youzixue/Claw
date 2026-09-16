"""当前风险投影的隔离验收；不把历史名单或代码前缀当身份许可。"""
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pandas as pd
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.stock_tagger import stock_tagger
from app.db.session import Base
from app.models.stock import StockTag, StockBlacklist, StockSpot
from app.risk.engine import RiskContext, RiskEngine, RiskLevel
from app.risk.rules import BlacklistRule, ObserveOnlyRule


def tag_row(code="000001", name="平安银行", **overrides):
    fields = dict(code=code, name=name, board_type=stock_tagger.get_board_type(code),
                  board_tag=stock_tagger.get_board_tag(stock_tagger.get_board_type(code)),
                  is_st=False, is_suspended=False, is_delisting=False,
                  is_ipo_recent=False, is_limit_up=False, is_limit_down=False)
    fields.update(overrides)
    return StockTag(**fields)


@pytest_asyncio.fixture
async def db_factory(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'identity.db'}")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield factory
    await engine.dispose()


@pytest.mark.parametrize("code", [None, "", 1, True, [], "600", "6000010", "600abc",
                                  "600001.SH", "sh600001", "６００００１", "600001 ",
                                  "999999", " 000001"])
def test_code_prefix_is_not_security_identity(code):
    assert stock_tagger.get_board_type(code) == "unknown"
    status = stock_tagger.resolve_status(code)
    assert not status["is_tradeable"]
    assert status["execution_authorized"] is False


@pytest.mark.parametrize("name,st,delisting", [
    ("ST瑞德", True, False), ("*ST测试", True, False), ("S*ST测试", True, False),
    (" SST测试 ", True, False), ("st测试", True, False), ("退市测试", False, True),
    ("测试退", False, True), ("退测试", False, True), ("TEST科技", False, False),
    ("平安银行", False, False),
])
def test_name_risks_not_overridden_by_false_flags(name, st, delisting):
    tag = tag_row(name=name)
    status = stock_tagger.resolve_status(tag.code, tag)
    assert status["is_st"] is st
    assert status["is_delisting"] is delisting
    assert status["is_tradeable"] is not (st or delisting)
    assert tag.board_tag == "tradeable"  # 纯投影，不改旧行。


@pytest.mark.parametrize("overrides,issue", [
    ({"name": None}, "missing_stock_name"),
    ({"name": "nan"}, "missing_stock_name"),
    ({"board_type": "star"}, "board_type_conflict"),
    ({"board_tag": "mystery"}, "unknown_board_tag"),
])
def test_inconsistent_identity_does_not_authorize(overrides, issue):
    status = stock_tagger.resolve_status("000001", tag_row(**overrides))
    assert issue in status["identity_issues"]
    assert not status["is_tradeable"]


@pytest.mark.parametrize("kwargs", [
    {"is_delisting": True}, {"is_st": True}, {"is_suspended": True},
    {"board_tag": "blocked"}, {"board_tag": "suspended"}, {"board_tag": "observe_only"},
])
def test_conflicting_tradeable_never_wins_existing_risk(kwargs):
    tag = tag_row(**kwargs)
    status = stock_tagger.resolve_status(tag.code, tag)
    assert not status["is_tradeable"]
    ctx = RiskContext(code=tag.code, board_tag=status["board_tag"],
                      is_st=status["is_st"], is_suspended=status["is_suspended"],
                      is_delisting=status["is_delisting"])
    assert any(rule.check(ctx).level == RiskLevel.BLOCK
               for rule in (BlacklistRule(), ObserveOnlyRule()))
    ctx.action = "sell"
    assert all(rule.check(ctx).level == RiskLevel.PASS
               for rule in (BlacklistRule(), ObserveOnlyRule()))


@pytest.mark.parametrize("code", ["300001", "688001", "920001"])
def test_wrong_tradeable_flag_cannot_make_observe_board_tradeable(code):
    status = stock_tagger.resolve_status(code, tag_row(code=code, board_tag="tradeable"))
    assert status["board_tag"] == "observe_only"
    assert not status["is_tradeable"]


@pytest.mark.parametrize("start,end,active", [
    (-1, None, True), (0, 0, True), (-5, -1, False), (1, None, False),
])
def test_blacklist_date_boundary_is_explicit(start, end, active):
    at = datetime(2026, 9, 14, 10)
    blacklist = StockBlacklist(code="000001", reason="manual", source="manual",
        start_date=at.date() + timedelta(days=start),
        end_date=None if end is None else at.date() + timedelta(days=end))
    status = stock_tagger.resolve_status("000001", tag_row(), blacklist=blacklist, at=at)
    assert status["active_blacklist"] is active
    assert status["is_tradeable"] is not active
    assert status["basis"] == "current_projection_not_historical_pit"


@pytest.mark.asyncio
async def test_mapping_refresh_cannot_clear_risk_or_ipo_and_limit_fields(db_factory):
    async with db_factory() as db:
        tag = tag_row(name="*ST原名", is_suspended=True, is_delisting=True,
                      ipo_date=date.today() - timedelta(days=10),
                      is_ipo_recent=True, is_limit_up=True, suspend_reason="既有原因")
        db.add(tag)
        await db.commit()
        await stock_tagger.batch_tag(db, [{"code": tag.code, "name": "新名称", "is_st": False}])
        await db.refresh(tag)
        assert tag.is_st and tag.is_delisting and tag.is_suspended
        assert tag.is_limit_up and tag.is_ipo_recent
        assert tag.suspend_reason == "既有原因"
        assert tag.board_tag == "suspended"


@pytest.mark.asyncio
async def test_stock_tagger_rejects_invalid_code_without_write(db_factory):
    async with db_factory() as db:
        with pytest.raises(ValueError, match="invalid_or_unknown"):
            await stock_tagger.tag_stock(db, "600abc", "异常")
        assert (await db.scalars(select(StockTag))).all() == []


@pytest.mark.asyncio
async def test_signal_filter_missing_conflicting_and_research_flags(db_factory):
    async with db_factory() as db:
        db.add_all([tag_row(), tag_row("002743", "ST测试"),
                    tag_row("600228", "正常名称", is_delisting=True),
                    tag_row("688001", "科创测试", board_tag="tradeable")])
        await db.commit()
        rows = [{"code": "000001", "name": "平安银行"},
                {"code": "002743", "name": "ST测试", "buy_allowed": True},
                {"code": "600228", "name": "正常名称"},
                {"code": "688001", "name": "科创测试"},
                {"code": "601123", "name": "身份未确认", "buy_allowed": True},
                {"code": "000001", "name": "错名称"}, {"name": "缺代码"},
                {"code": None}, {"code": []}]
        output = await stock_tagger.filter_signals(db, rows, mark_observe=False)
        assert len(output) == 7
        assert output[0]["is_tradeable"]
        assert all(not item["is_tradeable"] for item in output[1:])
        assert output[2]["buy_allowed"] is False
        assert "name_conflict" in output[3]["stock_status"]["identity_issues"]
        research = await stock_tagger.filter_signals(
            db, [rows[1]], exclude_blocked=False, mark_observe=False)
        assert research[0]["is_tradeable"] is False
        assert research[0]["buy_allowed"] is False
        assert rows[1]["buy_allowed"] is True and "stock_status" not in rows[1]


@pytest.mark.asyncio
async def test_blacklist_without_tag_is_still_excluded(db_factory):
    async with db_factory() as db:
        db.add(StockBlacklist(code="000001", reason="manual", source="manual",
                              start_date=date.today(), auto_expire=False))
        await db.commit()
        info = await stock_tagger.get_signal_filter(db)
        assert info["blocked_codes"] == ["000001"]
        assert await stock_tagger.filter_signals(db, [{"code": "000001"}]) == []


@pytest.mark.asyncio
async def test_old_candidate_name_cannot_hide_new_quote_st_risk(db_factory):
    async with db_factory() as db:
        db.add(tag_row("002743", "富煌钢构"))
        db.add(StockSpot(code="002743", name="ST富煌", price=10))
        await db.commit()
        filters = await stock_tagger.get_signal_filter(db)
        assert "002743" in filters["blocked_codes"]
        assert await stock_tagger.filter_signals(db, [
            {"code": "002743", "name": "富煌钢构", "buy_allowed": True}
        ]) == []
        status = await stock_tagger.load_status(db, "002743")
        assert status["is_st"] and not status["is_tradeable"]


def mock_status_fetch(monkeypatch, factory, frames):
    import app.data.scheduler as scheduler
    monkeypatch.setattr(scheduler.trade_calendar, "is_trade_day", AsyncMock(return_value=True))
    monkeypatch.setattr("pywencai.get", lambda *, query, loop: frames[query])
    monkeypatch.setattr(scheduler, "async_session", factory)
    success, failure = AsyncMock(), AsyncMock()
    monkeypatch.setattr(scheduler.data_quality_guard, "record_success", success)
    monkeypatch.setattr(scheduler.data_quality_guard, "record_failure", failure)
    return scheduler.DataScheduler(), success, failure


def risk_frames():
    frame = pd.DataFrame([{"股票代码": "000001.SZ", "股票简称": "ST测试"}])
    return {query: frame.copy() for query in ("ST股", "停牌", "*ST股", "北交所ST股")}


@pytest.mark.asyncio
async def test_status_positive_merge_preserves_all_existing_rows_and_blacklists(db_factory, monkeypatch):
    async with db_factory() as db:
        db.add_all([
            tag_row(name="旧名", is_limit_up=True, ipo_date=date(2026, 8, 1),
                    suspend_reason="旧原因"),
            tag_row("000002", "未出现在今日列表", is_st=True, is_delisting=True,
                    is_suspended=True, board_tag="suspended"),
            tag_row("000003", "普通标的"),
            StockBlacklist(code="000001", reason="人工禁止", source="manual",
                           start_date=date(2026, 8, 1), end_date=date(2026, 10, 1),
                           auto_expire=False),
            StockBlacklist(code="000002", reason="suspended", source="auto",
                           start_date=date(2026, 9, 1), auto_expire=True),
        ])
        await db.commit()
    scheduler, success, failure = mock_status_fetch(monkeypatch, db_factory, risk_frames())
    outcome = await scheduler._update_stock_status()
    assert outcome["status"] == "degraded" and outcome["positive_merge_status"] == "ok"
    assert outcome["record_count"] == 1  # 四个查询同代码，只计一次。
    assert outcome["automatic_clear_count"] == 0 and not outcome["coverage_verified"]
    success.assert_not_awaited()
    failure.assert_awaited_once()
    assert "全量覆盖和风险解除未核验" in failure.await_args.args[3]
    async with db_factory() as db:
        tags = {t.code: t for t in (await db.scalars(select(StockTag))).all()}
        assert len(tags) == 3
        assert tags["000001"].is_st and tags["000001"].is_suspended and tags["000001"].is_delisting
        assert tags["000001"].is_limit_up and tags["000001"].ipo_date == date(2026, 8, 1)
        assert tags["000001"].suspend_reason == "旧原因"
        assert tags["000002"].is_st and tags["000002"].is_delisting and tags["000002"].is_suspended
        assert tags["000003"].board_tag == "tradeable"
        bl = await db.get(StockBlacklist, "000001")
        assert (bl.reason, bl.source, bl.start_date, bl.end_date, bl.auto_expire) == (
            "人工禁止", "manual", date(2026, 8, 1), date(2026, 10, 1), False)
        assert (await db.get(StockBlacklist, "000002")).end_date is None


@pytest.mark.parametrize("bad", [
    None, pd.DataFrame([{"股票代码": "000001"}]),
    pd.DataFrame([{"股票代码": "000001", "股票简称": float("nan")}]),
    pd.DataFrame([{"股票代码": "000001.SH", "股票简称": "错市场"}]),
    pd.DataFrame([{"股票代码": "1", "股票简称": "短代码"}]),
    pd.DataFrame([{"股票代码": 1, "股票简称": "数值代码"}]),
    pd.DataFrame([{"股票代码": "600abc", "股票简称": "错代码"}]),
    pd.DataFrame([{"股票代码": "000001", "股票简称": "同码不同名"}]),
])
@pytest.mark.asyncio
async def test_malformed_partial_source_cannot_write_any_status(db_factory, monkeypatch, bad):
    frames = risk_frames()
    frames["停牌"] = bad
    scheduler, success, failure = mock_status_fetch(monkeypatch, db_factory, frames)
    outcome = await scheduler._update_stock_status()
    assert outcome["status"] == "failed"
    failure.assert_awaited_once()
    success.assert_not_awaited()
    async with db_factory() as db:
        assert (await db.scalars(select(StockTag))).all() == []
        assert (await db.scalars(select(StockBlacklist))).all() == []


@pytest.mark.asyncio
async def test_empty_individual_query_is_degraded_but_positive_risk_retained(db_factory, monkeypatch):
    frames = risk_frames()
    frames["停牌"] = pd.DataFrame()
    scheduler, success, failure = mock_status_fetch(monkeypatch, db_factory, frames)
    outcome = await scheduler._update_stock_status()
    assert outcome["status"] == "degraded"
    assert outcome["empty_queries"] == ["停牌"]
    failure.assert_awaited_once()
    success.assert_not_awaited()
    async with db_factory() as db:
        tag = await db.get(StockTag, "000001")
        assert tag.is_st and tag.is_delisting and not tag.is_suspended
        blacklist = await db.get(StockBlacklist, tag.code)
        assert blacklist.auto_expire is False


@pytest.mark.parametrize("calendar_value", [None, False, 1])
@pytest.mark.asyncio
async def test_status_requires_explicit_trade_day(db_factory, monkeypatch, calendar_value):
    import app.data.scheduler as module
    scheduler, success, failure = mock_status_fetch(monkeypatch, db_factory, risk_frames())
    monkeypatch.setattr(module.trade_calendar, "is_trade_day", AsyncMock(return_value=calendar_value))
    def unexpected_fetch(**kwargs):
        raise AssertionError("非明确交易日不得请求")
    monkeypatch.setattr("pywencai.get", unexpected_fetch)
    result = await scheduler._update_stock_status()
    assert result["status"] == "blocked"
    success.assert_not_awaited()
    failure.assert_not_awaited()


@pytest.mark.asyncio
async def test_status_query_crossing_midnight_does_not_write(db_factory, monkeypatch):
    import app.data.scheduler as module
    scheduler, success, failure = mock_status_fetch(monkeypatch, db_factory, risk_frames())
    calls = []
    class Clock:
        @staticmethod
        def now():
            calls.append(1)
            return datetime(2026, 9, 14, 23, 59) if len(calls) == 1 else datetime(2026, 9, 15)
    monkeypatch.setattr(module, "datetime", Clock)
    result = await scheduler._update_stock_status()
    assert result["status"] == "failed" and "跨日" in result["reason"]
    async with db_factory() as db:
        assert (await db.scalars(select(StockTag))).all() == []


@pytest.mark.asyncio
async def test_new_risk_does_not_reopen_old_expired_blacklist(db_factory, monkeypatch):
    async with db_factory() as db:
        db.add(StockBlacklist(code="000001", reason="旧人工原因", source="manual",
                              start_date=date(2026, 1, 1), end_date=date(2026, 2, 1),
                              auto_expire=False))
        await db.commit()
    scheduler, _, _ = mock_status_fetch(monkeypatch, db_factory, risk_frames())
    result = await scheduler._update_stock_status()
    assert result["positive_merge_status"] == "ok"
    async with db_factory() as db:
        bl = await db.get(StockBlacklist, "000001")
        assert bl.end_date == date(2026, 2, 1) and bl.reason == "旧人工原因"
        assert bl.start_date == date(2026, 1, 1) and bl.source == "manual"
        status = await stock_tagger.load_status(db, "000001")
        assert status["active_blacklist"] is False
        assert status["is_st"] and not status["is_tradeable"]


@pytest.mark.parametrize("risk_case", ["missing", "st_name", "delisting", "spot_st", "manual", "normal"])
@pytest.mark.asyncio
async def test_pretrade_uses_same_status_projection_without_orders(db_factory, monkeypatch, risk_case):
    from app.trading import service
    async with db_factory() as db:
        if risk_case != "missing":
            db.add(tag_row(name="ST测试" if risk_case == "st_name" else "平安银行",
                           is_delisting=risk_case == "delisting"))
        if risk_case == "spot_st":
            db.add(StockSpot(code="000001", name="ST平安", price=10))
        if risk_case == "manual":
            db.add(StockBlacklist(code="000001", reason="manual", source="manual",
                                  start_date=date.today()))
        await db.commit()
        monkeypatch.setattr(service, "_paper_account_context", AsyncMock(
            return_value=(100000, 100000, 0, {}, 0, {"id": 1, "name": "default", "status": "active"})))
        monkeypatch.setattr(service.sentiment_circuit_breaker, "get_current_state", AsyncMock(
            return_value=SimpleNamespace(trade_date=date.today(), phase="recovery", score=60, observed_at=None)))
        monkeypatch.setattr(service, "sentiment_quality_at", lambda *a, **k: ("ok", ""))
        engine = RiskEngine()
        engine.register(BlacklistRule())
        engine.register(ObserveOnlyRule())
        monkeypatch.setattr(service, "risk_engine", engine)
        outcome = await service._pre_trade_risk_check(
            db, service.SubmitOrderCommand(code="000001", side="buy", price=10, quantity=100))
        assert outcome["final_level"] == ("pass" if risk_case == "normal" else "block")
        assert outcome["stock_status"]["execution_authorized"] is False
        assert (await db.scalars(select(StockBlacklist))).all() == ([] if risk_case != "manual" else [
            await db.get(StockBlacklist, "000001")])
        from app.models.trading import TradeOrder, TradeFill
        assert (await db.scalars(select(TradeOrder))).all() == []
        assert (await db.scalars(select(TradeFill))).all() == []


@pytest.mark.asyncio
async def test_risk_api_request_cannot_substitute_missing_identity(db_factory, monkeypatch):
    from app.api.v1 import risk
    engine = RiskEngine()
    engine.register(BlacklistRule())
    engine.register(ObserveOnlyRule())
    monkeypatch.setattr(risk, "risk_engine", engine)
    async with db_factory() as db:
        result = await risk.risk_check(risk.RiskCheckRequest(
            code="000001", board_tag="tradeable", is_st=False, price=10, amount=100), db)
        assert result["final_level"] == "block"
        assert "missing_stock_tag" in result["stock_status"]["identity_issues"]
