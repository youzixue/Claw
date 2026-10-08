"""Isolated contract/DDL tests; never import app.main or submit orders."""
from datetime import datetime, timedelta
from dataclasses import replace
import importlib.util
from pathlib import Path
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from app.paper.portfolio_contract import (
    PortfolioPolicy, allocate_budget, make_signal_key, validate_signal_times,
)
from app.models.paper import PaperPortfolioSignal, PaperPortfolioDecision

NOW = datetime(2026, 9, 22, 13, 30)
POLICY = PortfolioPolicy(enabled=True, activation_at="2026-09-22T13:00:00")

def budget(**changes):
    args = dict(policy=POLICY, total_assets=50000, cash=50000, reserved_cash=0,
                held_exposure=0, pending_exposure=0, symbol_held_exposure=0,
                symbol_pending_exposure=0, occupied_symbols=0,daily_new_symbols=0,
                is_new_symbol=True, price=10, route_cap_ratio=.2)
    args.update(changes)
    return allocate_budget(**args)

def test_fee_reserved_and_route_not_raised():
    r = budget()
    assert r.allowed and r.amount == 900 and r.fee == 45 and r.reserve == 9045
    r = budget(route_cap_ratio=.1)
    assert r.amount == 400 and r.reserve == 4020
    assert budget(cash=1000).amount == 0
    assert budget(cash=1005).amount == 100

@pytest.mark.parametrize("field", ["cash","total_assets","reserved_cash","held_exposure",
    "pending_exposure","symbol_held_exposure","symbol_pending_exposure","price","route_cap_ratio"])
@pytest.mark.parametrize("bad", [float("nan"),float("inf"),-1,None])
def test_bad_numbers_fail_closed(field,bad):
    r=budget(**{field:bad})
    assert not r.allowed and r.reason_code == "invalid_budget_input"

def test_pending_counts_and_exposure():
    assert not budget(occupied_symbols=5).allowed
    assert not budget(daily_new_symbols=5).allowed
    assert not budget(held_exposure=39000,pending_exposure=1000).allowed
    assert not budget(symbol_pending_exposure=10000,pending_exposure=10000).allowed
    assert budget(reserved_cash=49500,pending_exposure=39000).amount == 0

def test_disabled_and_missing_activation():
    assert not budget(policy=PortfolioPolicy()).allowed
    assert not budget(policy=replace(POLICY,activation_at=None)).allowed
    assert not budget(policy=replace(POLICY,activation_at="2026-09-22")).allowed
    assert not budget(policy=replace(POLICY,max_symbol_ratio=.3)).allowed

def test_identity_stable_and_round_sensitive():
    args=dict(source="primary",origin_account="default",origin_version="v1",
              source_signal_id="s1",decision_round_id="r1")
    assert make_signal_key(**args)==make_signal_key(**args)
    assert make_signal_key(**args)!=make_signal_key(**{**args,"decision_round_id":"r2"})
    with pytest.raises(ValueError):
        make_signal_key(**{**args,"source_signal_id":None})

@pytest.mark.parametrize("field",["confirmed_at","as_of_at","observed_at"])
@pytest.mark.parametrize("value",[NOW+timedelta(seconds=1),NOW-timedelta(days=1),NOW-timedelta(minutes=10)])
def test_source_times(field,value):
    args=dict(policy=POLICY,confirmed_at=NOW,as_of_at=NOW,observed_at=NOW,now=NOW)
    args[field]=value
    assert validate_signal_times(**args) is not None

def test_time_valid_and_pre_activation():
    args=dict(policy=POLICY,confirmed_at=NOW,as_of_at=NOW,observed_at=NOW,now=NOW)
    assert validate_signal_times(**args) is None
    assert validate_signal_times(**{**args,"policy":replace(POLICY,activation_at="2026-09-22T13:30:01")})

def test_public_portfolio_adapter_contract(monkeypatch):
    from app.paper.portfolio_contract import portfolio_policy, portfolio_active, portfolio_version, entry_version
    from app.config.settings import settings
    monkeypatch.setattr(settings,"PAPER_PORTFOLIO_ENABLED",True)
    monkeypatch.setattr(settings,"PAPER_PORTFOLIO_ACTIVATION_AT","2026-09-22T13:30:00")
    assert portfolio_policy()["initial_capital"]==50000
    assert portfolio_version().startswith("s50k_v1_") and len(portfolio_version())<=31
    assert portfolio_active(NOW)
    assert not portfolio_active(NOW-timedelta(seconds=1))
    assert not portfolio_active(None)
    monkeypatch.setattr(settings,"PAPER_PORTFOLIO_ACTIVATION_AT","2026-09-22")
    assert not portfolio_active(NOW)
    monkeypatch.setattr(settings,"PAPER_PORTFOLIO_ENABLED",False)
    assert not portfolio_active(NOW)
    assert len(entry_version("origin"*100))<=64
    assert entry_version("v1")==entry_version("v1")
    assert entry_version("v1")!=entry_version("v2")
    with pytest.raises(ValueError):
        entry_version("")

def test_entry_version_reproduces_old_policy(monkeypatch):
    import app.paper.portfolio_contract as contract
    old_policy=contract.portfolio_version()
    original=contract.entry_version("origin_v1")
    monkeypatch.setattr(contract,"PORTFOLIO_VERSION","shared_50k_v2")
    assert contract.entry_version("origin_v1") != original
    assert contract.entry_version("origin_v1",policy_version=old_policy)==original
    assert len(contract.entry_version("origin_v1",policy_version="a"*31))==64
    for bad in ("", " ", "a"*32, 12):
        with pytest.raises(ValueError):
            contract.entry_version("origin_v1",policy_version=bad)

def test_policy_hash_tracks_constraints_not_enable(monkeypatch):
    from app.config.settings import settings
    from app.paper.portfolio_contract import portfolio_version
    monkeypatch.setattr(settings,"PAPER_PORTFOLIO_ENABLED",False)
    monkeypatch.setattr(settings,"PAPER_PORTFOLIO_ACTIVATION_AT","2026-09-22T13:30:00")
    original=portfolio_version()
    monkeypatch.setattr(settings,"PAPER_PORTFOLIO_ENABLED",True)
    assert portfolio_version()==original
    monkeypatch.setattr(settings,"PAPER_PORTFOLIO_ACTIVATION_AT","2026-09-22T05:30:00+00:00")
    assert portfolio_version()==original
    monkeypatch.setattr(settings,"PAPER_PORTFOLIO_MAX_EXPOSURE_RATIO",.7)
    assert portfolio_version()!=original
    monkeypatch.setattr(settings,"PAPER_PORTFOLIO_MAX_EXPOSURE_RATIO",.8)
    monkeypatch.setattr(settings,"PAPER_PORTFOLIO_ACTIVATION_AT","2026-09-22T13:31:00")
    assert portfolio_version()!=original

def test_signal_key_includes_account_id_and_policy():
    args=dict(source="primary",origin_account="default",origin_version="v1",
              source_signal_id="s1",decision_round_id="r1",origin_account_id=1,
              portfolio_version="policy1")
    original=make_signal_key(**args)
    assert make_signal_key(**{**args,"origin_account_id":2})!=original
    assert make_signal_key(**{**args,"portfolio_version":"policy2"})!=original
    assert make_signal_key(**args)==original

def test_signal_creation_fields():
    columns=PaperPortfolioSignal.__table__.columns
    assert not columns.source_signal_id.nullable
    assert not columns.created_at.nullable

def signal_values():
    return dict(id=1,signal_key="key",portfolio_version="shared_50k_v1",
                origin_account="default",origin_account_id=1,origin_version="v1",
                source="primary",code="600000",name="test",source_signal_id="s",
                confirmed_at=NOW,decision_round_id="r",as_of_at=NOW,observed_at=NOW,
                candidate_json="{}",entry_policy_json="{}",exit_policy_json="{}")

@pytest.mark.asyncio
async def test_models_sqlite_immutable_even_replace():
    engine=create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as c:
            await c.run_sync(lambda conn: PaperPortfolioSignal.__table__.create(conn))
            await c.run_sync(lambda conn: PaperPortfolioDecision.__table__.create(conn))
            await c.execute(PaperPortfolioSignal.__table__.insert().values(**signal_values()))
            await c.execute(PaperPortfolioDecision.__table__.insert().values(
                id=1,decision_key="d",signal_id=1,portfolio_version="shared_50k_v1",
                account_id=13,decision_round_id="r",as_of_at=NOW,observed_at=NOW,
                decision="rejected",reason_code="capacity",budget_json="{}"))
        for table,key in [("paper_portfolio_signal","signal_key"),("paper_portfolio_decision","decision_key")]:
            for sql in [f"UPDATE {table} SET {key}='changed' WHERE id=1",
                        f"DELETE FROM {table} WHERE id=1",
                        f"INSERT OR REPLACE INTO {table} SELECT * FROM {table} WHERE id=1"]:
                async with engine.begin() as c:
                    with pytest.raises(Exception,match="append-only"):
                        await c.execute(text(sql))
    finally:
        await engine.dispose()

@pytest.mark.parametrize("changes", [
    {"occupied_symbols":True},{"daily_new_symbols":1.5},{"is_new_symbol":None},
    {"is_new_symbol":False}, {"held_exposure":1000,"occupied_symbols":0},
    {"occupied_symbols":6,"is_new_symbol":False,"held_exposure":1000,"symbol_held_exposure":1000},
])
def test_inconsistent_or_overlimit_counts(changes):
    assert not budget(**changes).allowed

def test_existing_symbol_cap_and_fee_rounding():
    r=budget(is_new_symbol=False,occupied_symbols=1,held_exposure=9500,symbol_held_exposure=9500)
    assert not r.allowed
    r=budget(price=100,cash=10005)
    assert not r.allowed and r.reason_code == 'symbol_post_fee_whole_lot_limit'
    r=budget(price=7.9999,cash=50000)
    assert r.reserve >= r.notional+5

def test_partial_fee_budget_remainder_stays_executable_without_market_change():
    initial=budget()
    assert initial.amount==900 and initial.fee==45
    for filled_lots in range(1,9):
        paid=filled_lots*5
        held=filled_lots*1000
        remaining=900-filled_lots*100
        result=budget(total_assets=50000-paid,cash=50000-paid-held,
            held_exposure=held,symbol_held_exposure=held,occupied_symbols=1,is_new_symbol=False)
        assert result.amount>=remaining
        assert held+remaining*10 <= .2*(50000-paid-remaining/100*5)

def test_other_pending_fees_reduce_new_capacity():
    # cash 20k + holdings 30k; other pending 9k + 45 worst remaining fees.
    r=budget(cash=20000,held_exposure=30000,pending_exposure=9000,
             reserved_cash=9045,occupied_symbols=4)
    assert not r.allowed and r.reason_code=="portfolio_post_fee_whole_lot_limit"
    # The fifth lot exists on raw assets but not on assets net of all future fees.
    r=budget(cash=10000,held_exposure=40000,pending_exposure=0,reserved_cash=0,occupied_symbols=4)
    assert not r.allowed

@pytest.mark.parametrize("quantity,price,expected",[(100,10,5),(900,10,45),(1000,10,50),(200,1000,60)])
def test_worst_fee_is_per_100_share_slice(quantity,price,expected):
    from app.paper.portfolio_contract import worst_case_buy_fee
    assert worst_case_buy_fee(quantity=quantity,price=price,commission_rate=.0003,min_commission=5)==expected

@pytest.mark.parametrize("quantity,price",[(0,10),(150,10),(-100,10),(True,10),(100,float("inf"))])
def test_worst_fee_rejects_unknown_or_nonlot(quantity,price):
    from app.paper.portfolio_contract import worst_case_buy_fee
    with pytest.raises(ValueError):
        worst_case_buy_fee(quantity=quantity,price=price,commission_rate=.0003,min_commission=5)

def test_settings_explicit_and_failclosed():
    from app.config.settings import Settings
    from app.paper.portfolio_contract import policy_from_settings, policy_error
    settings=Settings(_env_file=None,PAPER_PORTFOLIO_ENABLED=False)
    assert policy_error(policy_from_settings(settings))=="portfolio_disabled"
    settings.PAPER_PORTFOLIO_ENABLED=True
    settings.PAPER_PORTFOLIO_ACTIVATION_AT=""
    assert policy_error(policy_from_settings(settings))=="invalid_portfolio_policy"

def test_timezone_and_observation_order():
    args=dict(policy=POLICY,confirmed_at=NOW,as_of_at=NOW,observed_at=NOW,now=NOW)
    assert validate_signal_times(**{**args,"confirmed_at":"2026-09-22T05:30:00+00:00"}) is None
    assert validate_signal_times(**{**args,"observed_at":NOW-timedelta(seconds=1)})=="invalid_observation_order"

def migration():
    path=Path(__file__).parents[1]/"alembic/versions/035_shared_paper_portfolio.py"
    spec=importlib.util.spec_from_file_location("portfolio_migration",path)
    mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
    return mod

@pytest.mark.asyncio
async def test_migration_new_and_existing_rejected():
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    mod=migration()
    assert mod.down_revision == "034_data_watermark_revisions"
    engine=create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as c:
            def upgrade(conn):
                with Operations.context(MigrationContext.configure(conn)):
                    mod.upgrade()
            await c.run_sync(upgrade)
            assert (await c.execute(text("SELECT count(*) FROM paper_portfolio_signal"))).scalar()==0
            await c.execute(PaperPortfolioSignal.__table__.insert().values(**signal_values()))
            for sql in ("UPDATE paper_portfolio_signal SET signal_key='bad'",
                        "DELETE FROM paper_portfolio_signal",
                        "INSERT OR REPLACE INTO paper_portfolio_signal SELECT * FROM paper_portfolio_signal"):
                with pytest.raises(Exception,match="append-only"):
                    await c.execute(text(sql))
            triggers=(await c.execute(text("SELECT name FROM sqlite_master WHERE type='trigger'"))).scalars().all()
            assert len(triggers)==6
            def parity(conn):
                from sqlalchemy import inspect
                for model in (PaperPortfolioSignal,PaperPortfolioDecision):
                    actual={x["name"]:x for x in inspect(conn).get_columns(model.__tablename__)}
                    assert set(actual)==set(model.__table__.columns.keys())
                    for column in model.__table__.columns:
                        assert actual[column.name]["nullable"]==column.nullable
                        assert str(actual[column.name]["type"])==str(column.type)
            await c.run_sync(parity)
            with pytest.raises(RuntimeError,match="existing"):
                await c.run_sync(upgrade)
    finally:
        await engine.dispose()
