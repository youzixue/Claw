"""Isolated contract tests; fixtures are synthetic, not vendor recovery evidence."""
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.config.settings import settings
from app.data.main_fund import current_main_fund_evidence, load_current_main_fund_map
from app.data.sources.tencent_source import TencentSource
from app.models.stock import FundFlow, StockSpot

DAY = date(2026, 9, 9)
NOW = datetime(2026, 9, 9, 10, 0, 10)


def fund(**overrides):
    result = dict(
        code="000001", name="隔离资金测试", trade_date=DAY,
        main_net_inflow=1_200_000.0, main_net_inflow_pct=2.5,
        big_net_inflow=None, mid_net_inflow=None, small_net_inflow=None,
        source="eastmoney", source_version="individual_fund_flow_v3_f124",
        source_quote_at=NOW - timedelta(seconds=10),
        received_at=NOW - timedelta(seconds=8), observed_at=NOW - timedelta(seconds=5),
    )
    result.update(overrides)
    return result


@pytest.mark.parametrize("amount,pct", [(120.0, 1.0), (0.0, 0.0), (-120.0, -1.0)])
def test_measured_values_keep_sign_and_real_zero(amount, pct):
    result = current_main_fund_evidence(
        fund(main_net_inflow=amount, main_net_inflow_pct=pct), trade_date=DAY, decision_at=NOW,
    )
    assert result["main_net_inflow"] == amount
    assert result["main_net_inflow_pct"] == pct
    assert result["big_net_inflow"] is None
    assert result["clock_status"] == "ok"
    assert "not_fund_calculation_time" in result["source_clock_basis"]


@pytest.mark.parametrize("overrides", [
    {"main_net_inflow": None}, {"main_net_inflow_pct": None},
    {"main_net_inflow": float("nan")}, {"main_net_inflow_pct": float("inf")},
    {"main_net_inflow": True}, {"main_net_inflow_pct": False},
    {"source": "tencent"}, {"source": "ths_via_akshare"},
    {"source_version": "individual_fund_flow_v1"},
    {"source_quote_at": None}, {"received_at": None}, {"observed_at": None},
    {"source_quote_at": NOW + timedelta(seconds=1)},
    {"received_at": NOW + timedelta(seconds=1)},
    {"observed_at": NOW + timedelta(seconds=1)},
    {"source_quote_at": NOW - timedelta(seconds=settings.FUND_FLOW_SOURCE_MAX_AGE_SEC + 1)},
    {"trade_date": DAY - timedelta(days=1)},
    {"source_quote_at": NOW - timedelta(days=1)},
])
def test_invalid_source_clock_and_missing_values_never_supply_funds(overrides):
    assert current_main_fund_evidence(fund(**overrides), trade_date=DAY, decision_at=NOW) is None


@pytest.mark.parametrize("field50", ["2593", "-40", "0", "", "-", "NaN", "1e20"])
def test_tencent_field50_never_becomes_money(field50):
    fields = [""] * 88
    for index, value in {1: "合成盘口测试", 2: "000001", 3: "10", 4: "9.9",
                         6: "1000", 9: "10", 10: "2608", 19: "10.01", 20: "15",
                         30: "20260909100000", 50: field50, 51: "9.95"}.items():
        fields[index] = value
    parsed = TencentSource()._parse_spot("000001", fields, received_at=NOW)
    assert parsed["main_net_inflow"] is None
    assert parsed["bid_depth_5"] - parsed["ask_depth_5"] == 2593
    assert parsed["volume"] == 1000
    assert parsed["amount"] == 995000
    assert parsed["source_quote_at"] == NOW - timedelta(seconds=10)


@pytest.mark.asyncio
async def test_current_read_uses_only_qualified_rows_without_autoflush(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'main-fund.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(FundFlow.__table__.create)
        await conn.run_sync(StockSpot.__table__.create)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with Session() as db:
            db.add_all([
                FundFlow(**fund()), FundFlow(**fund(code="000002", main_net_inflow=0)),
                FundFlow(**fund(code="000003", observed_at=NOW + timedelta(seconds=1))),
                FundFlow(**fund(code="000004", source="tencent")),
                StockSpot(code="000005", price=10, main_net_inflow=999999999),
            ])
            await db.commit()
            db.add(FundFlow(**fund(code="000006")))  # must remain unflushed
            result = await load_current_main_fund_map(db, trade_date=DAY, decision_at=NOW)
            assert set(result) == {"000001", "000002"}
            assert result["000002"]["main_net_inflow"] == 0
            assert len(db.new) == 1
            assert await load_current_main_fund_map(db, trade_date=DAY, decision_at=NOW, codes=[]) == {}
            assert set(await load_current_main_fund_map(
                db, trade_date=DAY, decision_at=NOW, codes=["000002"],
            )) == {"000002"}
            await db.rollback()
            legacy = await db.scalar(select(StockSpot).where(StockSpot.code == "000005"))
            assert legacy.main_net_inflow == 999999999  # no historical rewrite
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_spot_api_never_displays_or_sorts_by_legacy_book_difference(tmp_path, monkeypatch):
    from app.api.v1 import spot as api
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'spot-fund.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(FundFlow.__table__.create)
        await conn.run_sync(StockSpot.__table__.create)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW
    monkeypatch.setattr(api, "datetime", Clock)
    try:
        async with Session() as db:
            db.add_all([
                StockSpot(code="000001", price=10, main_net_inflow=-99999),
                StockSpot(code="000002", price=10, main_net_inflow=999999999),
                FundFlow(**fund(main_net_inflow=0, main_net_inflow_pct=0)),
            ])
            await db.commit()
            result = await api.spot_list(codes="", limit=50, sort_by="main_net_inflow", min_change=-100, db=db)
            assert [row["code"] for row in result["spots"]] == ["000001", "000002"]
            assert result["spots"][0]["main_net_inflow"] == 0
            assert result["spots"][1]["main_net_inflow"] is None
            assert result["spots"][1]["main_fund_status"] == "unknown"
            detail = await api.spot_detail("000002", db)
            assert detail["main_net_inflow"] is None
            assert detail["main_fund_source"] is None
    finally:
        await engine.dispose()


def test_promotion_never_falls_back_to_spot_or_estimates_missing_pct():
    from app.api.v1.promotion import _flow_value_from_spot_or_fund, _fund_range_score
    spot = SimpleNamespace(main_net_inflow=1e10, amount=1e11)
    assert _flow_value_from_spot_or_fund(spot) == (None, None)
    assert _flow_value_from_spot_or_fund(spot, SimpleNamespace(main_net_inflow=0, main_net_inflow_pct=0)) == (0, 0)
    assert _flow_value_from_spot_or_fund(spot, SimpleNamespace(main_net_inflow=-1e6)) == (-1e6, None)
    assert _flow_value_from_spot_or_fund(spot, SimpleNamespace(main_net_inflow=float("nan"))) == (None, None)
    assert _fund_range_score(None, ideal_low=0, ideal_high=8, min_value=-7, max_value=16) == 0
    assert _fund_range_score(0, ideal_low=0, ideal_high=8, min_value=-7, max_value=16) == 100


def test_score_preserves_actual_zero_pct_instead_of_recomputing_from_price():
    from app.signal.bull_score import BullScoreModel
    model = BullScoreModel()
    args = dict(code="000001", name="测试", price=10, main_net_inflow=1e8,
                main_net_inflow_pct=0, volume_ratio=1, turnover=1)
    low = model.score(**args, amount=1e8)
    high = model.score(**args, amount=1e11)
    assert low.dimensions["capital"] == high.dimensions["capital"]


@pytest.mark.parametrize("source,version", [
    ("tencent", "individual_fund_flow_v3_f124"),
    ("ths_via_akshare", "individual_total_fund_flow_ths_v1"),
    ("eastmoney", None),
])
def test_injected_anomaly_funds_need_a_real_mapping_not_only_fresh_clock(source, version):
    from app.signal.anomaly_scanner import AnomalyScanner
    item = AnomalyScanner._current_fund_evidence(
        fund(source=source, source_version=version), DAY, NOW,
    )
    assert item["is_stale"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("separator", ["T", " "])
async def test_sqlite_datetime_storage_separator_cannot_change_visibility(tmp_path, separator):
    from sqlalchemy import text
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'clock-encoding.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(FundFlow.__table__.create)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with Session() as db:
            for code, received, observed in [
                ("000001", NOW-timedelta(seconds=2), NOW-timedelta(seconds=1)),
                ("000002", NOW, NOW+timedelta(microseconds=1)),
            ]:
                await db.execute(text(
                    "INSERT INTO fund_flow (code,trade_date,main_net_inflow,main_net_inflow_pct,"
                    "source,source_version,source_quote_at,received_at,observed_at)"
                    "VALUES (:code,:day,0,0,'eastmoney','individual_fund_flow_v3_f124',:src,:recv,:obs)"
                ), {"code": code, "day": DAY.isoformat(),
                    "src": (NOW-timedelta(seconds=3)).isoformat(sep=separator),
                    "recv": received.isoformat(sep=separator),
                    "obs": observed.isoformat(sep=separator)})
            await db.commit()
            current = await load_current_main_fund_map(db, trade_date=DAY, decision_at=NOW)
            assert set(current) == {"000001"}  # future one-microsecond evidence stays rejected
            stored = (await db.execute(text("SELECT observed_at FROM fund_flow WHERE code='000001'"))).scalar()
            assert stored[10] == separator  # projection does not migrate raw clock text
    finally:
        await engine.dispose()


def test_only_original_fund_dependent_accounts_version_the_fund_contract():
    from app.paper.experiment import experiment_parameters, PRICE_PATH_ACCOUNTS
    for name in ("default", "promotion", "mainline", "auction"):
        assert experiment_parameters(name)["MAIN_FUND_DATA_POLICY"].endswith("no_spot")
    for name in PRICE_PATH_ACCOUNTS:
        assert "MAIN_FUND_DATA_POLICY" not in experiment_parameters(name)

